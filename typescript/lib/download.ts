/**
 * N_m3u8DL-RE download wrapper — VOD & LIVE modes.
 *
 * VOD (vod.live.eplus.jp):
 *   --live-perform-as-vod  → wait for all segments
 *   --mux-after-done mp4   → final MP4 with proper moov atom
 *
 * LIVE (stream.live.eplus.jp):
 *   --live-pipe-mux        → real-time TS output (NO --mux-after-done)
 *   PotPlayer opens the growing .ts file — TS natively supports progressive read
 *
 * Ported from main.py run_download() with mode-aware parameter tuning.
 */
import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'

// ── Types ──────────────────────────────────────────────────

interface BaseOptions {
  urlMpd: string
  cookieStr: string
  key: string
  downloaderPath: string
  ffmpegPath: string
  /** Shaka Packager is the recommended engine for real-time CENC decryption */
  shakaPackagerPath: string
  outputDir: string
  tempDir: string
  threadCount?: number
  /** Log file for N_m3u8DL-RE's own output */
  logFilePath?: string
}

interface VodOptions extends BaseOptions {
  /** Recording time limit (HH:mm:ss) — only for VOD treated as live endpoint */
  recordLimit?: string
}

interface LiveOptions extends BaseOptions {
  /** Recording time limit (HH:mm:ss) */
  recordLimit?: string
  /** Manually set live playlist refresh interval (seconds) */
  waitTime?: number
}

// ── Helpers ────────────────────────────────────────────────

function timestamp(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}`
}

/**
 * Detect download mode from MPD URL.
 *   vod.live.eplus.jp   → VOD (archive/replay)
 *   stream.live.eplus.jp → LIVE
 */
export function detectMode(url: string): 'vod' | 'live' {
  if (url.includes('stream.live.eplus.jp'))
    return 'live'
  if (url.includes('vod.live.eplus.jp'))
    return 'vod'
  // Fallback: treat unknown as VOD (safer — won't hang waiting for live)
  console.warn(`⚠️  Unknown host in MPD URL, defaulting to vod mode: ${url}`)
  return 'vod'
}

/** Build args common to both modes */
function buildCommonArgs(opts: BaseOptions): string[] {
  const ts = timestamp()
  const saveName = `eplus_drm_${ts}`
  const args: string[] = [
    opts.urlMpd,
    '--save-name',
    saveName,
    '--save-dir',
    resolve(opts.outputDir),
    '--tmp-dir',
    resolve(opts.tempDir),
    '--download-retry-count',
    '5',
    '--auto-select',
    '--thread-count',
    String(opts.threadCount ?? 16),
    '--mp4-real-time-decryption',
    '--decryption-engine',
    'SHAKA_PACKAGER',
    '--decryption-binary-path',
    resolve(opts.shakaPackagerPath),
    '--check-segments-count',
    '--ffmpeg-binary-path',
    resolve(opts.ffmpegPath),
    '-H',
    `Cookie: ${opts.cookieStr}`,
    '-mt',
    '--del-after-done',
    '--key',
    opts.key,
    '--log-level',
    'INFO',
  ]

  if (opts.logFilePath) {
    args.push('--log-file-path', resolve(opts.logFilePath))
  }

  return args
}

function spawn(downloaderPath: string, args: string[]): void {
  const result = spawnSync(downloaderPath, args, {
    stdio: 'inherit',
    shell: false,
  })

  if (result.error) {
    throw new Error(`Failed to spawn N_m3u8DL-RE: ${result.error.message}`)
  }

  if (result.status !== 0) {
    throw new Error(`N_m3u8DL-RE exited with code ${result.status}`)
  }
}

// ── VOD ────────────────────────────────────────────────────

/**
 * Download VOD (archive/replay).
 *
 * Output: eplus_drm_<timestamp>.mp4
 *
 * Flags:
 *   --live-perform-as-vod  ensures all segments are collected before "done"
 *   --mux-after-done mp4   proper MP4 container with moov atom (good for archiving)
 */
export function downloadVod(options: VodOptions): void {
  const { recordLimit } = options

  const args = buildCommonArgs(options)

  // VOD-specific
  args.push('--live-perform-as-vod')
  args.push('--mux-after-done', 'format=mp4')

  if (recordLimit) {
    args.push('--live-record-limit', recordLimit)
  }

  console.log('╔══════════════════════════════════════════════════════╗')
  console.log('║  📼 VOD 模式 — 存档下载                               ║')
  console.log('╚══════════════════════════════════════════════════════╝')
  console.log(`  🔗 ${options.urlMpd}`)
  console.log(`  📁 输出目录: ${resolve(options.outputDir)}`)
  console.log(`  📝 日志:     ${options.logFilePath ?? '(none)'}`)
  console.log(`  🏷️  容器:     MP4 (moov atom finalized on completion)`)
  if (recordLimit)
    console.log(`  ⏱️  录制上限: ${recordLimit}`)
  console.log()

  spawn(options.downloaderPath, args)

  console.log('\n✅ VOD 下载完成')
}

// ── LIVE ───────────────────────────────────────────────────

/**
 * Download LIVE stream.
 *
 * Output: eplus_drm_<timestamp>.ts  (growing TS file)
 *
 * Flags:
 *   --live-pipe-mux         pipe decrypted segments to ffmpeg → single TS file
 *   --live-keep-segments    keep raw segments (recovery if crash)
 *   NO --mux-after-done     keeps TS container — natively progressive,
 *                           PotPlayer reads new data without manual refresh
 *
 * After recording ends, convert to MP4 with:
 *   ffmpeg -i output.ts -c copy output.mp4
 */
export function downloadLive(options: LiveOptions): void {
  const { recordLimit, waitTime } = options

  const args = buildCommonArgs(options)

  // LIVE-specific
  args.push('--live-real-time-merge')
  args.push('--live-pipe-mux')
  args.push('--live-keep-segments')
  // ⚠️ NO --mux-after-done → keeps TS output

  if (recordLimit) {
    args.push('--live-record-limit', recordLimit)
  }
  if (waitTime != null) {
    args.push('--live-wait-time', String(waitTime))
  }

  console.log('╔══════════════════════════════════════════════════════╗')
  console.log('║  🔴 LIVE 模式 — 实时录制                              ║')
  console.log('╚══════════════════════════════════════════════════════╝')
  console.log(`  🔗 ${options.urlMpd}`)
  console.log(`  📁 输出目录: ${resolve(options.outputDir)}`)
  console.log(`  📝 日志:     ${options.logFilePath ?? '(none)'}`)
  console.log(`  🏷️  容器:     TS (growing — PotPlayer 可直接打开)`)
  if (recordLimit)
    console.log(`  ⏱️  录制上限: ${recordLimit}`)
  if (waitTime)
    console.log(`  🔄 刷新间隔:  ${waitTime}s`)
  console.log(`  💡 录制结束后转 MP4: ffmpeg -i <name>.ts -c copy <name>.mp4`)
  console.log()

  spawn(options.downloaderPath, args)

  console.log('\n✅ LIVE 录制结束')
}
