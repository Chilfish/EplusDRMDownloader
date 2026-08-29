/**
 * CLI entry — full Eplus DRM download pipeline (TypeScript reference implementation).
 *
 * Mode is auto-detected from MPD URL:
 *   vod.live.eplus.jp   → VOD (archive/replay)
 *   stream.live.eplus.jp → LIVE
 * Override: --mode vod|live
 *
 * Usage (run from the repo root, or any directory):
 *   bun ./typescript/main.ts                 # Full pipeline: extract keys → download
 *   bun ./typescript/main.ts --keys-only     # Only extract keys, skip download
 *   bun ./typescript/main.ts --mode live     # Force live mode
 *   bun ./typescript/main.ts --urlMpd=... --cookieStr=... --authUrl=...
 *
 * Reads the project-root `.env` (shared with the Python version). CLI args
 * override `.env` values. All relative paths (.env, bin/, Downloads, Temp,
 * logs) are resolved against the project root, so the working directory does
 * not matter.
 */
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { config } from 'dotenv'
import { detectMode, downloadLive, downloadVod } from './lib/download.js'
import { getEplusKeys } from './lib/eplus.js'

// Project root is one level above this script's directory:
//   <repo root>/typescript/main.ts → <repo root>
// Binaries (N_m3u8DL-RE, ffmpeg, shaka-packager, *.wvd) live in <repo root>/bin
// and are shared with the Python version.
const ROOT = resolve(import.meta.dirname, '..')

// Read the project-root .env (shared with the Python implementation).
config({ path: resolve(ROOT, '.env') })

// Resolve a path against the project root unless it is already absolute.
const resolveRoot = (p: string): string => (isAbsolute(p) ? p : resolve(ROOT, p))

// ── Simple argv parser ─────────────────────────────────────
function parseArgs(): Record<string, string> {
  const args: Record<string, string> = {}
  for (const arg of process.argv.slice(2)) {
    if (arg.startsWith('--')) {
      const eqIdx = arg.indexOf('=')
      if (eqIdx > 2) {
        args[arg.slice(2, eqIdx)] = arg.slice(eqIdx + 1)
      }
      else {
        args[arg.slice(2)] = 'true'
      }
    }
  }
  return args
}

// ── Ensure directory exists ────────────────────────────────
function ensureDir(dir: string): void {
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true })
  }
}

// ── Logging ────────────────────────────────────────────────
function log(msg: string): void {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19)
  console.log(`[${ts}] ${msg}`)
}

// ── Main ───────────────────────────────────────────────────
async function main() {
  const args = parseArgs()
  const keysOnly = args['keys-only'] === 'true'

  // ── Resolve params: CLI args override .env ───────────────
  const urlMpd = args.urlMpd ?? process.env.URL_MPD
  const cookieStr = args.cookieStr ?? process.env.COOKIE_MPD
  const authUrl = args.authUrl ?? process.env.AUTH_URL
  const wvdPath = args.wvdPath ?? process.env.WVD_PATH

  // Validate required
  const missing: string[] = []
  if (!urlMpd)
    missing.push('URL_MPD (--urlMpd)')
  if (!cookieStr)
    missing.push('COOKIE_MPD (--cookieStr)')
  if (!authUrl)
    missing.push('AUTH_URL (--authUrl)')
  if (!wvdPath)
    missing.push('WVD_PATH (--wvdPath)')

  if (missing.length > 0) {
    console.error('❌ Missing required params:')
    for (const m of missing) console.error(`   - ${m}`)
    console.error('\nSet them in .env or pass via CLI:')
    console.error(
      '  bun ./typescript/main.ts --urlMpd=... --cookieStr=... --authUrl=... --wvdPath=./bin/xxx.wvd',
    )
    process.exit(1)
  }

  // ── Detect mode ──────────────────────────────────────────
  const mode = args.mode ?? detectMode(urlMpd!)
  log(`Mode: ${mode.toUpperCase()}`)
  log(`MPD:  ${urlMpd}`)

  // ── Download-related params (defaults in the shared bin/) ─
  const downloaderPath = args.downloader ?? process.env.N_M3U8DL_PATH ?? resolveRoot('./bin/N_m3u8DL-RE.exe')
  const ffmpegPath = args.ffmpeg ?? process.env.FFMPEG_PATH ?? resolveRoot('./bin/ffmpeg.exe')
  const shakaPackagerPath = args.shakaPackager ?? process.env.SHAKA_PACKAGER_PATH ?? resolveRoot('./bin/shaka-packager.exe')
  const outputDir = args.outputDir ?? process.env.OUTPUT_DIR ?? './Downloads'
  const tempDir = args.tempDir ?? process.env.TEMP_DIR ?? './Temp'

  // Mode-specific
  const recordLimit = args.recordLimit ?? process.env.RECORD_LIMIT
  const waitTime = args.waitTime ?? process.env.LIVE_WAIT_TIME
  const logDir = process.env.LOG_DIR ?? './logs'

  try {
    // ── Load WVD ───────────────────────────────────────────
    log(`Loading WVD: ${wvdPath}`)
    const wvdBuffer = readFileSync(resolveRoot(wvdPath!))

    // ── Step 1: Extract keys ───────────────────────────────
    log('Extracting keys...')

    const result = await getEplusKeys({
      urlMpd: urlMpd!,
      cookieStr: cookieStr!,
      authUrl: authUrl!,
      wvdBuffer,
    })

    log(`KID:  ${result.kid}`)
    log(`Key:  ${result.selectedKey}`)
    log(`Base: ${result.base ?? '(none)'}`)

    if (keysOnly) {
      console.log('\n── Key data ───────────────────────────────────────────')
      console.log(JSON.stringify(result, null, 2))
      console.log(
        '\n── N_m3u8DL-RE command ───────────────────────────────',
      )
      console.log(
        `N_m3u8DL-RE "${result.mpdUrl}" --key "${result.selectedKey}"`,
      )
      return
    }

    if (!result.selectedKey) {
      throw new Error('No key available for download')
    }

    // ── Step 2: Download ──────────────────────────────────
    ensureDir(resolveRoot(outputDir))
    ensureDir(resolveRoot(tempDir))
    ensureDir(resolveRoot(logDir))

    const baseOpts = {
      urlMpd: result.mpdUrl,
      cookieStr: cookieStr!,
      key: result.selectedKey,
      downloaderPath: resolveRoot(downloaderPath),
      ffmpegPath: resolveRoot(ffmpegPath),
      shakaPackagerPath: resolveRoot(shakaPackagerPath),
      outputDir: resolveRoot(outputDir),
      tempDir: resolveRoot(tempDir),
      logFilePath: resolve(ROOT, logDir, `n_m3u8dl-re_${Date.now()}.log`),
    }

    if (mode === 'live') {
      downloadLive({
        ...baseOpts,
        recordLimit: recordLimit ?? undefined,
        waitTime: waitTime ? Number(waitTime) : undefined,
      })
    }
    else {
      downloadVod({
        ...baseOpts,
        recordLimit: recordLimit ?? undefined,
      })
    }

    log('All done!')
  }
  catch (err) {
    console.error('')
    console.error('❌ Failed:', err instanceof Error ? err.message : String(err))
    process.exit(1)
  }
}

main()
