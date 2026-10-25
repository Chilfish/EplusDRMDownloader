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
import { mergeCookies, parseCookies } from './parse-cookies.js'
import { createPsshFromKid } from './pssh.js'

// ── Constants ──────────────────────────────────────────────
const LICENSE_URL
  = 'https://lic.drmtoday.com/license-proxy-widevine/cenc/?specConform=true'

const MPD_KID_RE = /cenc:default_KID="(?<kid>[^"]+)"/
const MPD_BASE_RE
  = /https:\/\/(?:vod|stream)\.live\.eplus\.jp\/out\/v1\/(?<base>[^/]*)\//
const AUTH_APP_ID_RE = /\/api\/stream\/(?<appId>[^/]+)\/get_auth_token_drm/
const AUTH_SID_RE = /[?&]sid=(?<sid>[^&]+)/
const PLAYER_APP_RE = /var\s+app\s*=\s*(\{[\s\S]*?\})\s*;\s*<\/script>/
const PLAYER_APP_ID_RE = /"app_id"\s*:\s*"([^"]+)"/
const PLAYER_ASSET_ID_RE = /"assetId"\s*:\s*"([^"]+)"/
const PLAYER_SESSION_RE = /var\s+streamSession\s*=\s*['"]([^'"]+)['"]/
const PLAYER_MPD_RE = /var\s+channels_dash\s*=\s*\["([^"]+)"\]/
const APP_NAME_RE = /"app_name"\s*:\s*"((?:[^"\\]|\\.)*)"/
const PAGE_TITLE_RE = /<title>([^<]*)<\/title>/i
/** eplus returns the stream path base inside the signed policy Resource */
const MPD_PATH_BASE_RE = /\/out\/v1\/([^/]+)\//

// ── Types ──────────────────────────────────────────────────
export interface EplusKeysInput {
  /** MPD manifest URL — derived from PLAYER_URL when omitted */
  urlMpd?: string
  /** Cookie string for Eplus requests (session cookies are enough) */
  cookieStr: string
  /** Auth token URL — derived from PLAYER_URL when omitted */
  authUrl?: string
  /**
   * Player page URL (`/ex/player?ib=...` or `?ep=...`). When given, it is
   * fetched once to obtain the title, fresh `sid`, MPD URL, `channel_id`
   * and freshly signed CloudFront cookies.
   */
  playerUrl?: string
  /** WVD (Widevine Device) file as base64 (or raw Buffer) */
  wvdBase64?: string
  wvdBuffer?: Buffer
  /** Optional logger for per-step diagnostics */
  log?: (msg: string) => void
  /** Dump full response bodies (default: truncated preview) */
  verbose?: boolean
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
  /** Stream title read from the player page (when playerUrl was given) */
  title?: string
  /** Cookie string actually used (session + freshly signed CloudFront) */
  cookieStr: string
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

// ── Diagnostics helpers ────────────────────────────────────

/** Hide auth tokens in logged bodies. */
function maskSecrets(text: string): string {
  return text.replace(
    /("(?:auth_token|token|license|session_?id|user_?id)"\s*:\s*")([^"]{4})[^"]*/gi,
    (_, prefix: string, head: string) => `${prefix}${head}…`,
  )
}

/** Truncated, secret-masked body preview for logs. */
function preview(text: string, max = 600): string {
  const masked = maskSecrets(text)
  const shown = masked.length > max ? `${masked.slice(0, max)}…` : masked
  return `${shown.trim()} [${text.length} chars]`
}

/** Preview a binary response only when it looks like text (JSON/XML error page). */
function previewBuffer(buf: Buffer, max = 600): string {
  const head = buf.subarray(0, 64).toString('latin1')
  if (!/^[\s{[]|^<\?xml|^<html/i.test(head)) {
    return `<binary ${buf.length} bytes, starts ${JSON.stringify(head.slice(0, 16))}>`
  }
  return preview(buf.toString('utf8'), max)
}

/** Show which response headers matter for auth debugging. */
function describeHeaders(res: Response): string {
  const keys = ['content-type', 'content-length', 'set-cookie', 'location']
  return keys
    .map(k => [k, res.headers.get(k)] as const)
    .filter(([, v]) => v != null)
    .map(([k, v]) => `${k}=${k === 'set-cookie' ? '<present>' : v}`)
    .join(' ')
}

/**
 * Check whether the `sid` inside AUTH_URL is still alive. Eplus refreshes the
 * signed CloudFront cookies for an active playback session via
 * `/api/stream/<appId>/status?sid=<sid>`, which answers `{"error":true}` once
 * the sid (the player page's `streamSession`) is no longer valid.
 */
async function probeStreamSession(
  authUrl: string,
  headers: Record<string, string>,
  log: (msg: string) => void,
): Promise<void> {
  const appId = AUTH_APP_ID_RE.exec(authUrl)?.groups?.appId
  const sid = AUTH_SID_RE.exec(authUrl)?.groups?.sid
  if (!appId || !sid) {
    log('[probe] AUTH_URL does not match /api/stream/<id>/get_auth_token_drm?...&sid=... — skipped')
    return
  }

  const probeUrl = `https://live.eplus.jp/api/stream/${appId}/status?sid=${encodeURIComponent(sid)}`
  try {
    const res = await fetch(probeUrl, { headers })
    const text = await res.text()
    const rejected = /"error"\s*:\s*true/.test(text)
    log(`[probe] GET ${probeUrl} → HTTP ${res.status} · sid ${rejected ? 'REJECTED (expired/invalid)' : 'accepted'} · body: ${preview(text, 200)}`)
  }
  catch (err) {
    log(`[probe] GET ${probeUrl} failed: ${err instanceof Error ? err.message : String(err)}`)
  }
}

/** Replace (or append) the `sid` query param of an AUTH_URL. */
function withSid(authUrl: string, sid: string): string {
  if (AUTH_SID_RE.test(authUrl)) {
    return authUrl.replace(/([?&]sid=)[^&]*/, (_m, prefix: string) => `${prefix}${encodeURIComponent(sid)}`)
  }
  return `${authUrl}${authUrl.includes('?') ? '&' : '?'}sid=${encodeURIComponent(sid)}`
}

export interface PlayerSession {
  /** Stream title (`app_name`) */
  title?: string
  /** Fresh `streamSession` — usable as the AUTH_URL `sid` */
  sid?: string
  /** `app_id` from the page (`/api/stream/<appId>/...`) */
  appId?: string
  /** `drmEncryptKey.assetId` — the `channel_id` query value */
  assetId?: string
  /** DASH manifest URL from `channels_dash` */
  mpdUrl?: string
  /** Freshly signed CloudFront cookies (Cookie header value) */
  cookies?: string
  /** Expiry of the signed cookies (epoch ms) */
  cookiesExpireAt?: number
}

/** Pick the CloudFront cookie set whose signed policy covers `mpdUrl`. */
function pickCloudFrontCookies(
  setCookies: string[],
  mpdUrl: string | undefined,
): { cookies?: string, expiresAt?: number } {
  const base = mpdUrl ? MPD_PATH_BASE_RE.exec(mpdUrl)?.[1] : undefined
  const groups: { pairs: string[], resource: string, expiresAt?: number }[] = []

  for (const raw of setCookies) {
    const pair = raw.split(';')[0]
    const name = pair.slice(0, pair.indexOf('='))
    if (!name.startsWith('CloudFront-'))
      continue

    const value = pair.slice(pair.indexOf('=') + 1)
    const expires = Date.parse(/expires=([^;]+)/i.exec(raw)?.[1] ?? '')

    if (name === 'CloudFront-Policy') {
      let resource = ''
      let policyExpiry: number | undefined
      try {
        const decoded = Buffer.from(value, 'base64').toString('utf8')
        const json = JSON.parse(decoded.slice(decoded.indexOf('{'), decoded.lastIndexOf('}') + 1))
        resource = json.Statement?.[0]?.Resource ?? ''
        const epoch = json.Statement?.[0]?.Condition?.DateLessThan?.['AWS:EpochTime']
        policyExpiry = typeof epoch === 'number' ? epoch * 1000 : undefined
      }
      catch { /* leave unmatched */ }
      groups.push({ pairs: [pair], resource, expiresAt: policyExpiry ?? (Number.isNaN(expires) ? undefined : expires) })
      continue
    }

    // Signature / Key-Pair-Id belong to the most recent Policy
    const group = groups.at(-1)
    if (group && !group.pairs.some(p => p.startsWith(`${name}=`)))
      group.pairs.push(pair)
  }

  if (groups.length === 0)
    return {}

  const chosen = (base && groups.find(g => g.resource.includes(base))) || groups[0]
  const cookies = chosen.pairs.join('; ')
  // Keep non-CloudFront cookies (e.g. a rotated ci_session) from the response too.
  const others = setCookies
    .filter(c => !c.startsWith('CloudFront-'))
    .map(c => c.split(';')[0])
  return {
    cookies: others.length ? `${cookies}; ${others.join('; ')}` : cookies,
    expiresAt: chosen.expiresAt,
  }
}

/**
 * Read a full playback session from an eplus player page.
 *
 * One request to the page the user is watching (`/ex/player?ib=...` or
 * `?ep=...`) yields everything the pipeline needs:
 *   - `app_name`          → output file name
 *   - `streamSession`     → fresh one-shot `sid` for `get_auth_token_drm`
 *   - `assetId`           → the `channel_id` query value
 *   - `channels_dash`     → the MPD URL
 *   - CloudFront cookies  → signed segment access, valid for ~1 hour
 *
 * Never throws: a failed/expired page yields an empty session so the caller
 * can fall back to the values from `.env`.
 */
export async function fetchPlayerSession(
  playerUrl: string,
  cookieStr: string,
  log: (msg: string) => void = () => {},
): Promise<PlayerSession> {
  try {
    const res = await fetch(playerUrl, {
      headers: {
        'User-Agent':
          'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0',
        'Cookie': cookieStr,
        'Accept': 'text/html,application/xhtml+xml',
      },
    })
    const html = await res.text()
    log(`[player] GET ${playerUrl} → HTTP ${res.status} · ${html.length} chars`)

    const session: PlayerSession = {}
    session.sid = PLAYER_SESSION_RE.exec(html)?.[1]
    session.appId = PLAYER_APP_ID_RE.exec(html)?.[1]
    session.assetId = PLAYER_ASSET_ID_RE.exec(html)?.[1]
    session.mpdUrl = PLAYER_MPD_RE.exec(html)?.[1]?.replace(/\\\//g, '/')

    const appJson = PLAYER_APP_RE.exec(html)?.[1]
    if (appJson) {
      try {
        const app = JSON.parse(appJson) as { app_name?: string }
        session.title = app.app_name
      }
      catch {
        log('[player] var app found but is not valid JSON — falling back to regex')
      }
    }
    if (!session.title) {
      const quoted = APP_NAME_RE.exec(html)?.[1]
      if (quoted) {
        try {
          session.title = JSON.parse(`"${quoted}"`) as string
        }
        catch { /* keep undefined */ }
      }
    }

    const signed = pickCloudFrontCookies(
      res.headers.getSetCookie ? res.headers.getSetCookie() : [],
      session.mpdUrl,
    )
    session.cookies = signed.cookies
    session.cookiesExpireAt = signed.expiresAt

    if (session.title)
      log(`[player] app_name: ${session.title}`)
    else
      log(`[player] no app_name on page (page <title>: ${PAGE_TITLE_RE.exec(html)?.[1]?.trim() || '(none)'}) — check PLAYER_URL / Cookie`)

    if (session.sid)
      log(`[player] streamSession: ${session.sid.slice(0, 8)}… (${session.sid.length} chars)`)
    else
      log('[player] no streamSession on page — AUTH_URL sid will be used as-is')

    if (session.mpdUrl)
      log(`[player] MPD: ${session.mpdUrl}`)
    if (session.cookiesExpireAt)
      log(`[player] signed cookies valid until ${new Date(session.cookiesExpireAt).toISOString()}`)
    else
      log('[player] no signed CloudFront cookies issued')

    return session
  }
  catch (err) {
    log(`[player] failed: ${err instanceof Error ? err.message : String(err)} — continuing with .env values`)
    return {}
  }
}

// ── Main ───────────────────────────────────────────────────

export async function getEplusKeys(input: EplusKeysInput): Promise<EplusKeysOutput> {
  const { cookieStr } = input
  const log = input.log ?? (() => {})
  const maxBody = input.verbose ? 4000 : 600

  // ── Step 0: Player page → title, sid, MPD, channel_id, cookies ─
  // `sid` is one-shot: AUTH_URL stops working after it has been used once.
  // Each player-page load issues a new `streamSession` and fresh signed
  // CloudFront cookies (valid ~1h), so the pipeline self-heals.
  const player: PlayerSession = input.playerUrl
    ? await fetchPlayerSession(input.playerUrl, cookieStr, log)
    : {}

  const effectiveCookies = mergeCookies(cookieStr, player.cookies)
  const cookies = parseCookies(effectiveCookies)
  log(`[cookie] ${Object.keys(cookies).length} cookie(s) in use: ${Object.keys(cookies).join(', ') || '(none)'}`)

  // Build fetch headers
  const headers: Record<string, string> = {
    'User-Agent':
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0',
    'Cookie': effectiveCookies,
  }

  const urlMpd = input.urlMpd ?? player.mpdUrl
  if (!urlMpd)
    throw new Error('No MPD URL — set URL_MPD or PLAYER_URL')

  const derivedAuthUrl = (player.appId && player.assetId && player.sid)
    ? `https://live.eplus.jp/api/stream/${player.appId}/get_auth_token_drm?channel_id=${encodeURIComponent(player.assetId)}&sid=${encodeURIComponent(player.sid)}`
    : undefined
  const authUrl = input.authUrl ?? derivedAuthUrl
  if (!authUrl)
    throw new Error('No auth URL — set AUTH_URL or PLAYER_URL')

  if (!input.urlMpd)
    log('[mpd] using MPD URL from PLAYER_URL')
  if (!input.authUrl && derivedAuthUrl)
    log('[auth] using auth URL derived from PLAYER_URL')

  const authCandidates = [authUrl]
  if (player.sid) {
    const refreshed = withSid(authUrl, player.sid)
    if (refreshed !== authUrl) {
      authCandidates.unshift(refreshed)
      log(`[auth] using fresh sid ${player.sid.slice(0, 8)}… from PLAYER_URL`)
    }
  }
  if (player.appId && AUTH_APP_ID_RE.exec(authUrl)?.groups?.appId !== player.appId)
    log(`[auth] note: PLAYER_URL app_id ${player.appId} differs from AUTH_URL — AUTH_URL wins`)

  // ── Step 1: Get auth token ───────────────────────────────
  let authToken: string | undefined
  let authText = ''
  let authStatus = 0
  let authStatusText = ''

  for (const [i, candidate] of authCandidates.entries()) {
    log(`[auth] GET ${candidate}${i > 0 ? ' (fallback to AUTH_URL sid)' : ''}`)
    const authRes = await fetch(candidate, { headers })
    authText = await authRes.text()
    authStatus = authRes.status
    authStatusText = authRes.statusText
    log(`[auth] HTTP ${authRes.status} ${authRes.statusText} · ${describeHeaders(authRes) || '(no relevant headers)'} · ${authText.length} chars`)
    log(`[auth] body: ${preview(authText, maxBody)}`)
    if (!authRes.ok)
      continue

    try {
      const json = JSON.parse(authText) as { auth_token?: string }
      if (json.auth_token) {
        authToken = json.auth_token
        break
      }
    }
    catch {
      log(`[auth] response is not JSON (content-type: ${authRes.headers.get('content-type') ?? 'unknown'})`)
    }
  }

  if (!authToken) {
    const hint = authText.includes('"error"')
      ? `Server rejected the request ({"error":true}). Set PLAYER_URL so a fresh sid is fetched automatically, or re-copy AUTH_URL and its Cookie from the get_auth_token_drm request in DevTools while the player is running.${
        cookies.ci_session
          ? ''
          : ' The Cookie has no ci_session entry — copy the Cookie header of that same get_auth_token_drm request (and keep the CloudFront-* cookies from the .mpd request), not only the one from the .mpd request.'
      }`
      : 'Unexpected auth response shape — check whether the API changed or the session cookie is missing.'
    await probeStreamSession(authCandidates[0], headers, log)
    throw new Error(
      `No auth_token in auth response (HTTP ${authStatus} ${authStatusText}) — body: ${preview(authText, maxBody)}\n   ${hint}`,
    )
  }
  log(`[auth] got auth_token (${authToken.length} chars)`)

  // ── Step 2: Fetch MPD ────────────────────────────────────
  log(`[mpd] GET ${urlMpd}`)
  const mpdRes = await fetch(urlMpd, { headers })
  const mpdText = await mpdRes.text()
  const finalUrl = mpdRes.url // may differ after redirect
  log(`[mpd] HTTP ${mpdRes.status} ${mpdRes.statusText} · ${mpdText.length} chars · final URL: ${finalUrl}`)
  if (!mpdRes.ok) {
    throw new Error(
      `MPD fetch failed: HTTP ${mpdRes.status} ${mpdRes.statusText} — body: ${preview(mpdText, maxBody)}`,
    )
  }
  if (!mpdText.includes('cenc:default_KID')) {
    throw new Error(
      `MPD contains no cenc:default_KID (content-type: ${mpdRes.headers.get('content-type') ?? 'unknown'}) — head: ${preview(mpdText, maxBody)}`,
    )
  }

  const base = extractBase(finalUrl)
  const kid = extractKid(mpdText)
  log(`[mpd] KID=${kid} base=${base ?? '(none)'}`)

  // ── Step 3: Build PSSH ───────────────────────────────────
  const pssh = createPsshFromKid(kid)
  log(`[pssh] ${pssh.length} chars (base64)`)

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
  log(`[wvd] device loaded (${wvdBuffer.length} bytes), session created`)

  // NOTE: pywidevine does NOT set a service certificate by default.
  // The built-in COMMON_SERVICE_CERTIFICATE in node-widevine triggers a
  // protobuf parse error ("illegal tag: field no 0 wire type 2"), so skip it.

  // ── Step 5: Generate challenge & request license ─────────
  const challenge = session.generateChallenge()
  log(`[license] POST ${LICENSE_URL} · challenge ${challenge.length} bytes · X-Dt-Auth-Token ${authToken.length} chars`)

  const licenseRes = await fetch(LICENSE_URL, {
    method: 'POST',
    headers: {
      'accept': '*/*',
      'Connection': 'keep-alive',
      'X-Dt-Auth-Token': authToken,
    },
    body: challenge,
  })

  const licenseBody = Buffer.from(await licenseRes.arrayBuffer())
  log(`[license] HTTP ${licenseRes.status} ${licenseRes.statusText} · ${describeHeaders(licenseRes) || '(no relevant headers)'} · ${licenseBody.length} bytes`)

  if (!licenseRes.ok) {
    throw new Error(
      `License request failed [HTTP ${licenseRes.status} ${licenseRes.statusText}]: ${previewBuffer(licenseBody, maxBody)}`,
    )
  }

  // ── Step 6: Parse license → extract keys ─────────────────
  let rawKeys: ReturnType<typeof session.parseLicense>
  try {
    rawKeys = session.parseLicense(licenseBody)
  }
  catch (err) {
    throw new Error(
      `Failed to parse license response: ${err instanceof Error ? err.message : String(err)} — body: ${previewBuffer(licenseBody, maxBody)}`,
    )
  }
  log(`[license] parsed ${rawKeys.length} key entr(y/ies)`)

  // Filter undefined entries (parseLicense returns sparse array)
  const keyContainers = rawKeys.filter(
    (k): k is { kid: string, key: string } => k != null,
  )

  if (keyContainers.length === 0) {
    throw new Error(
      `No valid keys in license response — body: ${previewBuffer(licenseBody, maxBody)}`,
    )
  }
  log(`[license] keys: ${keyContainers.map(k => k.kid).join(', ')}`)

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
    title: player.title,
    cookieStr: effectiveCookies,
  }
}
