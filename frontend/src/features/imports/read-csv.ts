/**
 * Reading a CSV in the browser, before anything is sent.
 *
 * The API takes the file as text in a JSON body rather than as a multipart
 * upload (`python-multipart` is deliberately not a dependency), so the browser
 * does the decoding and has to answer three questions the server otherwise
 * would: what encoding is this, is it small enough to send as one JSON string,
 * and did the decoding actually work.
 *
 * Encoding is the one that bites. `FileReader.readAsText` and `File.text()`
 * both assume UTF-8 and replace bytes they cannot read with U+FFFD, so a CSV
 * exported by a Windows tool imports as `Ã‰lodie` or `<27>lodie` with no error
 * anywhere. The backend's own fallback never sees those bytes, because by the
 * time the text reaches it the damage is done (issue #79). So the bytes are
 * read raw, the encoding is worked out here, and the person is told which one
 * was used and can pick another.
 */

/** The encodings the wizard offers. Every one is a label `TextDecoder` accepts. */
export const ENCODINGS = [
  'utf-8',
  'windows-1252',
  'windows-1250',
  'iso-8859-15',
  'macintosh',
  'utf-16le',
  'utf-16be',
] as const

export type Encoding = (typeof ENCODINGS)[number]

export const ENCODING_LABELS: Record<Encoding, string> = {
  'utf-8': 'UTF-8',
  'windows-1252': 'Windows-1252 (Western European)',
  'windows-1250': 'Windows-1250 (Central European)',
  'iso-8859-15': 'ISO-8859-15 (Latin-9)',
  macintosh: 'Mac OS Roman',
  'utf-16le': 'UTF-16, little-endian',
  'utf-16be': 'UTF-16, big-endian',
}

/** How an encoding was arrived at, for the line the mapping screen shows. */
export type EncodingReason = 'bom' | 'utf-8' | 'utf-16-nulls' | 'fallback' | 'chosen'

export const ENCODING_REASONS: Record<EncodingReason, string> = {
  bom: 'from the byte-order mark at the start of the file',
  'utf-8': 'the bytes are valid UTF-8',
  'utf-16-nulls': 'every other byte is zero, which is how UTF-16 looks',
  fallback: 'the bytes are not valid UTF-8, so the usual Windows export encoding was assumed',
  chosen: 'you chose it',
}

/**
 * The most decoded text the wizard sends in one request.
 *
 * The body is one JSON string, held twice in browser memory and read whole by
 * the backend, so the cap is a real limit and not a formality. Eight million
 * characters is roughly 8 MB of CSV: a LinkedIn Connections export runs about
 * 130 bytes a row, so this covers a network of about 60,000 connections, past
 * anything LinkedIn's 30,000-connection ceiling can produce. It also sits well
 * under the backend's own 32,000,000-character cap (`MAX_IMPORT_CHARS`), so a
 * file that passes here never fails server-side after the whole body has been
 * uploaded.
 */
export const MAX_IMPORT_CHARACTERS = 8_000_000

/** The character a decoder leaves where bytes made no sense. */
const REPLACEMENT = '�'

/** Bytes sniffed for byte-order marks and for the UTF-16 zero pattern. */
const SNIFF_BYTES = 1024

export interface Decoded {
  ok: true
  filename: string
  content: string
  encoding: Encoding
  reason: EncodingReason
  /** Characters no encoding could make sense of; above zero, the guess is wrong. */
  replacements: number
}

export type FileRead = Decoded | { ok: false; reason: string }

function approximateMegabytes(characters: number): string {
  return `${(characters / 1_000_000).toFixed(1)} MB`
}

function startsWith(bytes: Uint8Array, ...prefix: number[]): boolean {
  return prefix.every((byte, index) => bytes[index] === byte)
}

/** True when `bytes` decode as UTF-8 with nothing left over. */
function isUtf8(bytes: Uint8Array): boolean {
  try {
    new TextDecoder('utf-8', { fatal: true }).decode(bytes)
    return true
  } catch {
    return false
  }
}

/**
 * Which encoding a file is in, and how that was decided.
 *
 * In order: a byte-order mark settles it outright; UTF-16 without one is given
 * away by the run of zero bytes at every second position; bytes that are valid
 * UTF-8 are UTF-8, because nothing else produces valid UTF-8 by accident; and
 * anything left is treated as Windows-1252, which is what Excel and the older
 * Windows CRMs write and which can decode any byte at all.
 */
export function sniffEncoding(buffer: ArrayBuffer): { encoding: Encoding; reason: EncodingReason } {
  const bytes = new Uint8Array(buffer)
  if (startsWith(bytes, 0xef, 0xbb, 0xbf)) return { encoding: 'utf-8', reason: 'bom' }
  if (startsWith(bytes, 0xff, 0xfe)) return { encoding: 'utf-16le', reason: 'bom' }
  if (startsWith(bytes, 0xfe, 0xff)) return { encoding: 'utf-16be', reason: 'bom' }

  const sample = bytes.subarray(0, SNIFF_BYTES)
  let evenZeros = 0
  let oddZeros = 0
  for (let index = 0; index < sample.length; index += 1) {
    if (sample[index] !== 0) continue
    if (index % 2 === 0) evenZeros += 1
    else oddZeros += 1
  }
  // ASCII text in UTF-16 is one zero byte per character, all on the same side.
  const pairs = Math.floor(sample.length / 2)
  if (pairs > 0 && oddZeros > pairs / 2 && evenZeros === 0) {
    return { encoding: 'utf-16le', reason: 'utf-16-nulls' }
  }
  if (pairs > 0 && evenZeros > pairs / 2 && oddZeros === 0) {
    return { encoding: 'utf-16be', reason: 'utf-16-nulls' }
  }

  if (isUtf8(bytes)) return { encoding: 'utf-8', reason: 'utf-8' }
  return { encoding: 'windows-1252', reason: 'fallback' }
}

/**
 * Read `file` as text, sniffing the encoding unless one is given.
 *
 * `as` comes from the person overriding the guess on the mapping screen; the
 * file is read again rather than kept in memory, because it is a local file and
 * the browser has it.
 */
export async function readCsvFile(file: File, as?: Encoding): Promise<FileRead> {
  const buffer = await file.arrayBuffer()
  if (buffer.byteLength === 0) {
    return { ok: false, reason: `${file.name} is empty.` }
  }

  const sniffed =
    as === undefined ? sniffEncoding(buffer) : { encoding: as, reason: 'chosen' as const }
  let content: string
  try {
    content = new TextDecoder(sniffed.encoding).decode(buffer)
  } catch {
    // Only a label this runtime does not know can land here, and every label in
    // ENCODINGS is one the WHATWG encoding standard requires.
    return {
      ok: false,
      reason: `This browser cannot decode ${ENCODING_LABELS[sniffed.encoding]}.`,
    }
  }

  if (content.length > MAX_IMPORT_CHARACTERS) {
    return {
      ok: false,
      reason:
        `${file.name} holds ${approximateMegabytes(content.length)} of text, over the ` +
        `${approximateMegabytes(MAX_IMPORT_CHARACTERS)} an import accepts in one request. ` +
        'Split it into smaller files and import them one after another.',
    }
  }

  let replacements = 0
  for (const character of content) {
    if (character === REPLACEMENT) replacements += 1
  }

  return {
    ok: true,
    filename: file.name,
    content,
    encoding: sniffed.encoding,
    reason: sniffed.reason,
    replacements,
  }
}
