/**
 * Eplus DRM key extraction — ported from main.py
 *
 * Flow:
 * 1. Get auth_token from AUTH_URL
 * 2. Fetch MPD, extract KID from cenc:default_KID
 * 3. Build Widevine PSSH box from KID
 * 4. Init Widevine device from WVD, create session
 * 5. Generate license challenge → POST to license server
 * 6. Parse license response → extract content keys
 * 7. Return MPD url + keys to caller
 */
import { LicenseType, Widevine } from 'widevine'
import { parseCookies } from './parse-cookies.js'
import { createPsshFromKid } from './pssh.js'

// ── Constants ──────────────────────────────────────────────
const LICENSE_URL
  = 'https://lic.drmtoday.com/license-proxy-widevine/cenc/?specConform=true'

const MPD_KID_RE = /cenc:default_KID="(?<kid>[^"]+)"/
const MPD_BASE_RE
  = /https:\/\/(?:vod|stream)\.live\.eplus\.jp\/out\/v1\/(?<base>[^/]*)\//

// ── Types ──────────────────────────────────────────────────
export interface EplusKeysInput {
  /** MPD manifest URL */
  urlMpd: string
  /** Cookie string for Eplus requests */
  cookieStr: string
  /** Auth token URL from Eplus DRM API */
  authUrl: string
  /** WVD (Widevine Device) file as base64 (or raw Buffer) */
  wvdBase64?: string
  wvdBuffer?: Buffer
}

export interface EplusKeysOutput {
  /** The MPD URL used */
  mpdUrl: string
  /** Extracted base value from stream URL (may be null) */
  base?: string
  /** Key ID (UUID format) */
  kid: string
  /** Generated PSSH (base64) */
  pssh: string
  /** Decryption keys: "kid_hex:key_hex" format (compatible with N_m3u8DL-RE --key) */
  keys: string[]
  /** Raw key containers for programmatic use */
  keyContainers: { kid: string, key: string }[]
  /** Primary key for --key flag (kid_hex:key_hex) */
  selectedKey?: string
}

// ── Helpers ────────────────────────────────────────────────

function extractKid(mpdText: string): string {
  const m = MPD_KID_RE.exec(mpdText)
  if (!m?.groups?.kid) {
    throw new Error('Could not find cenc:default_KID in MPD')
  }
  return m.groups.kid
}

function extractBase(url: string): string | undefined {
  const m = MPD_BASE_RE.exec(url)
  return m?.groups?.base
}

// ── Main ───────────────────────────────────────────────────

export async function getEplusKeys(input: EplusKeysInput): Promise<EplusKeysOutput> {
  const { urlMpd, cookieStr, authUrl } = input
  const cookies = parseCookies(cookieStr)

  // Build fetch headers
  const headers: Record<string, string> = {
    'User-Agent':
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0',
    'Cookie': cookieStr,
  }

  // ── Step 1: Get auth token ───────────────────────────────
  const authRes = await fetch(authUrl)
  if (!authRes.ok) {
    throw new Error(`Auth token request failed: ${authRes.status}`)
  }
  const authJson = (await authRes.json()) as { auth_token?: string }
  if (!authJson.auth_token) {
    throw new Error('No auth_token in auth response')
  }
  const authToken = authJson.auth_token

  // ── Step 2: Fetch MPD ────────────────────────────────────
  const mpdRes = await fetch(urlMpd, { headers })
  if (!mpdRes.ok) {
    throw new Error(`MPD fetch failed: ${mpdRes.status}`)
  }
  const mpdText = await mpdRes.text()
  const finalUrl = mpdRes.url // may differ after redirect

  const base = extractBase(finalUrl)
  const kid = extractKid(mpdText)

  // ── Step 3: Build PSSH ───────────────────────────────────
  const pssh = createPsshFromKid(kid)

  // ── Step 4: Init Widevine device ─────────────────────────
  let wvdBuffer: Buffer
  if (input.wvdBuffer) {
    wvdBuffer = input.wvdBuffer
  }
  else if (input.wvdBase64) {
    wvdBuffer = Buffer.from(input.wvdBase64, 'base64')
  }
  else {
    throw new Error('Either wvdBase64 or wvdBuffer is required')
  }

  const device = Widevine.initWVD(wvdBuffer)
  const session = device.createSession(Buffer.from(pssh, 'base64'), LicenseType.STREAMING)

  // NOTE: pywidevine does NOT set a service certificate by default.
  // The built-in COMMON_SERVICE_CERTIFICATE in node-widevine triggers a
  // protobuf parse error ("illegal tag: field no 0 wire type 2"), so skip it.

  // ── Step 5: Generate challenge & request license ─────────
  const challenge = session.generateChallenge()

  const licenseRes = await fetch(LICENSE_URL, {
    method: 'POST',
    headers: {
      'accept': '*/*',
      'Connection': 'keep-alive',
      'X-Dt-Auth-Token': authToken,
    },
    body: challenge,
  })

  if (!licenseRes.ok) {
    const errText = await licenseRes.text().catch(() => '(empty)')
    throw new Error(`License request failed [${licenseRes.status}]: ${errText}`)
  }

  // ── Step 6: Parse license → extract keys ─────────────────
  const licenseBody = Buffer.from(await licenseRes.arrayBuffer())
  const rawKeys = session.parseLicense(licenseBody)

  // Filter undefined entries (parseLicense returns sparse array)
  const keyContainers = rawKeys.filter(
    (k): k is { kid: string, key: string } => k != null,
  )

  if (keyContainers.length === 0) {
    throw new Error('No valid keys in license response')
  }

  // Apply same key selection logic as Python:
  // - Single key → use it
  // - Multiple keys → use the second one (index 1)
  const selected = keyContainers.length > 1 ? keyContainers[1] : keyContainers[0]

  // Format: "kid_hex:key_hex" (compatible with N_m3u8DL-RE --key)
  const keys = keyContainers.map(k => `${k.kid}:${k.key}`)

  return {
    mpdUrl: finalUrl,
    base,
    kid,
    pssh,
    keys,
    keyContainers,
    selectedKey: selected ? `${selected.kid}:${selected.key}` : undefined,
  }
}
