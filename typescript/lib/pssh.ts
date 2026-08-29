/**
 * PSSH (Protection System Specific Header) construction
 * Ported from Python pywidevine logic in main.py
 *
 * Widevine System ID: edef8ba9-79d6-4ace-a3c8-27dcd51d21ed
 */
const WIDEVINE_SYSTEM_ID = 'edef8ba979d64acea3c827dcd51d21ed'

/** Magic padding from the original Python code */
const MAGIC_PADDING = '48e3dc959b06'

/**
 * Build a Widevine PSSH box from a KID (Key ID).
 *
 * The PSSH box structure:
 * - 4 bytes: box size
 * - 4 bytes: "pssh"
 * - 4 bytes: version/flags (0x00000000)
 * - 16 bytes: Widevine system ID
 * - 4 bytes: data size
 * - N bytes: data (Google PlayReady header + KID + magic padding)
 *
 * @param kid - The Key ID (with or without hyphens, 32 hex chars)
 * @returns Base64-encoded PSSH string
 */
export function createPsshFromKid(kid: string): string {
  const kidClean = kid.replace(/-/g, '')
  if (kidClean.length !== 32) {
    throw new Error(`Invalid KID length: expected 32 hex chars, got ${kidClean.length}`)
  }

  // Build the byte array
  const parts: Buffer[] = [
    // PSSH box header
    Buffer.from('000000387073736800000000', 'hex'), // size(4) + "pssh"(4) + version/flags(4)
    // Widevine system ID
    Buffer.from(WIDEVINE_SYSTEM_ID, 'hex'),
    // Data length (0x18 = 24 bytes)
    Buffer.from('00000018', 'hex'),
    // Google PlayReady header
    Buffer.from('1210', 'hex'),
    // KID
    Buffer.from(kidClean, 'hex'),
    // Magic padding from original code
    Buffer.from(MAGIC_PADDING, 'hex'),
  ]

  return Buffer.concat(parts).toString('base64')
}
