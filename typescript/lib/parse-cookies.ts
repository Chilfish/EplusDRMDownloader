/**
 * Parse a cookie string into a key-value Record.
 *
 * @param cookieStr - Raw Cookie header value (e.g. "a=1; b=2")
 * @returns Parsed cookie object
 */
export function parseCookies(cookieStr: string): Record<string, string> {
  const cookies: Record<string, string> = {}
  for (const item of cookieStr.split(';')) {
    const trimmed = item.trim()
    const eqIdx = trimmed.indexOf('=')
    if (eqIdx > 0) {
      cookies[trimmed.slice(0, eqIdx)] = trimmed.slice(eqIdx + 1)
    }
  }
  return cookies
}

/**
 * Serialize cookie Record back to a Cookie header string.
 */
export function serializeCookies(cookies: Record<string, string>): string {
  return Object.entries(cookies)
    .map(([k, v]) => `${k}=${v}`)
    .join('; ')
}
