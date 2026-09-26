import { describe, expect, it } from 'vitest'

import openapiText from '../../../openapi.json?raw'

import { MAX_IMPORT_CHARACTERS, readCsvFile, sniffEncoding } from './read-csv'

interface ContentSchema {
  maxLength?: number
}

/** The backend's own cap on `content`, from the committed OpenAPI export. */
function backendCap(schema: string): number {
  const openapi = JSON.parse(openapiText) as {
    components: { schemas: Record<string, { properties: { content?: ContentSchema } }> }
  }
  const cap = openapi.components.schemas[schema]?.properties.content?.maxLength
  if (cap === undefined) throw new Error(`${schema}.content has no maxLength in openapi.json`)
  return cap
}

/** `name,note\nÉlodie,café\n` in one encoding or another. */
function accented(bytes: number[]): File {
  return new File([new Uint8Array(bytes)], 'people.csv')
}

const WINDOWS_1252 = [
  ...[0x6e, 0x61, 0x6d, 0x65, 0x0a], // name\n
  0xc9,
  0x6c,
  0x6f,
  0x64,
  0x69,
  0x65,
  0x0a, // Élodie\n
]

const UTF_8 = [...[0x6e, 0x61, 0x6d, 0x65, 0x0a], 0xc3, 0x89, 0x6c, 0x6f, 0x64, 0x69, 0x65, 0x0a]

describe('sniffEncoding', () => {
  it('trusts a byte-order mark', () => {
    expect(sniffEncoding(new Uint8Array([0xef, 0xbb, 0xbf, 0x61]).buffer)).toEqual({
      encoding: 'utf-8',
      reason: 'bom',
    })
    expect(sniffEncoding(new Uint8Array([0xff, 0xfe, 0x61, 0x00]).buffer)).toEqual({
      encoding: 'utf-16le',
      reason: 'bom',
    })
    expect(sniffEncoding(new Uint8Array([0xfe, 0xff, 0x00, 0x61]).buffer)).toEqual({
      encoding: 'utf-16be',
      reason: 'bom',
    })
  })

  it('spots UTF-16 with no mark by its zero bytes', () => {
    const little = new Uint8Array([0x6e, 0x00, 0x61, 0x00, 0x6d, 0x00, 0x65, 0x00])
    expect(sniffEncoding(little.buffer)).toEqual({ encoding: 'utf-16le', reason: 'utf-16-nulls' })

    const big = new Uint8Array([0x00, 0x6e, 0x00, 0x61, 0x00, 0x6d, 0x00, 0x65])
    expect(sniffEncoding(big.buffer)).toEqual({ encoding: 'utf-16be', reason: 'utf-16-nulls' })
  })

  it('calls valid UTF-8 bytes UTF-8, and anything else Windows-1252', () => {
    expect(sniffEncoding(new Uint8Array(UTF_8).buffer)).toEqual({
      encoding: 'utf-8',
      reason: 'utf-8',
    })
    expect(sniffEncoding(new Uint8Array(WINDOWS_1252).buffer)).toEqual({
      encoding: 'windows-1252',
      reason: 'fallback',
    })
  })
})

describe('readCsvFile', () => {
  it('reads a UTF-8 file as UTF-8', async () => {
    const read = await readCsvFile(new File(['name\nRosalind Quillfeather\n'], 'people.csv'))
    expect(read).toMatchObject({
      ok: true,
      filename: 'people.csv',
      content: 'name\nRosalind Quillfeather\n',
      encoding: 'utf-8',
      replacements: 0,
    })
  })

  it('reads a Windows export as Windows-1252 instead of mangling it (issue #79)', async () => {
    const read = await readCsvFile(accented(WINDOWS_1252))
    expect(read.ok).toBe(true)
    expect(read).toMatchObject({
      content: 'name\nÉlodie\n',
      encoding: 'windows-1252',
      reason: 'fallback',
      replacements: 0,
    })
  })

  it('takes a chosen encoding over the sniffed one', async () => {
    const sniffed = await readCsvFile(accented(UTF_8))
    expect(sniffed).toMatchObject({ content: 'name\nÉlodie\n', encoding: 'utf-8' })

    const forced = await readCsvFile(accented(UTF_8), 'windows-1252')
    expect(forced).toMatchObject({ encoding: 'windows-1252', reason: 'chosen' })
    // The same bytes read the wrong way: one character becomes two. Which two
    // depends on the runtime's windows-1252 table, so only the count is pinned.
    expect(forced.ok && forced.content).not.toBe('name\nÉlodie\n')
    expect(forced.ok && forced.content.length).toBe(sniffed.ok ? sniffed.content.length + 1 : 0)
  })

  it('tells a UTF-8 file with a few bad bytes from a Windows one', async () => {
    // Plenty of accents that decode, plus one byte that does not: a damaged
    // UTF-8 file. Reading it as Windows-1252 would mangle every one of them,
    // and Windows-1252 never reports a replacement to say so.
    const damaged = new Uint8Array([...UTF_8, 0x9d, ...UTF_8.slice(5), ...UTF_8.slice(5)])
    const read = await readCsvFile(accented([...damaged]))
    expect(read).toMatchObject({ encoding: 'windows-1252', reason: 'fallback', replacements: 0 })
    expect(read.ok && read.damagedUtf8).toBe(true)
    expect(read.ok && read.utf8Damage).toBe(1)
  })

  it('does not call a real Windows export damaged UTF-8', async () => {
    // Every accented byte here is invalid UTF-8, so none would survive: that is
    // a Windows-1252 file, not a damaged one.
    const read = await readCsvFile(accented(WINDOWS_1252))
    expect(read.ok && read.damagedUtf8).toBe(false)
  })

  it('counts characters no encoding could make sense of', async () => {
    // Valid UTF-8 read as UTF-16LE: an odd byte count leaves a lone unit.
    const read = await readCsvFile(accented(UTF_8), 'utf-16be')
    expect(read.ok).toBe(true)
    expect(read.ok && read.replacements).toBeGreaterThan(0)
  })

  it('strips a byte-order mark rather than importing it as a column name', async () => {
    const read = await readCsvFile(accented([0xef, 0xbb, 0xbf, 0x6e, 0x61, 0x6d, 0x65]))
    expect(read).toMatchObject({ content: 'name', encoding: 'utf-8', reason: 'bom' })
  })

  it('refuses a file that is only a byte-order mark as empty, not as content', async () => {
    for (const mark of [
      [0xff, 0xfe],
      [0xfe, 0xff],
      [0xef, 0xbb, 0xbf],
    ]) {
      const read = await readCsvFile(new File([new Uint8Array(mark)], 'bom.csv'))
      expect(read.ok).toBe(false)
      expect(read.ok === false && read.reason).toMatch(/bom\.csv is empty/)
    }
  })

  it('does not call a UTF-8 file broken for carrying a U+FFFD of its own', async () => {
    const read = await readCsvFile(new File(['name\nPlaceholder \uFFFD kept\n'], 'own.csv'))
    expect(read).toMatchObject({ ok: true, encoding: 'utf-8', replacements: 0 })
  })

  it('refuses UTF-32 by its mark rather than reading it as UTF-16', async () => {
    const little = [0xff, 0xfe, 0x00, 0x00, 0x6e, 0x00, 0x00, 0x00]
    const big = [0x00, 0x00, 0xfe, 0xff, 0x00, 0x00, 0x00, 0x6e]
    for (const bytes of [little, big]) {
      const read = await readCsvFile(new File([new Uint8Array(bytes)], 'wide.csv'))
      expect(read.ok).toBe(false)
      expect(read.ok === false && read.reason).toMatch(/UTF-32/)
    }
  })

  it('refuses an empty file', async () => {
    const read = await readCsvFile(new File([], 'nothing.csv'))
    expect(read.ok).toBe(false)
    expect(read.ok === false && read.reason).toMatch(/is empty/)
  })

  it('refuses a file too large to send in one JSON body, without saying 8 MB is over 8 MB', async () => {
    const file = new File(['a'.repeat(MAX_IMPORT_CHARACTERS + 1)], 'huge.csv')
    const read = await readCsvFile(file)
    expect(read.ok).toBe(false)
    const reason = read.ok === false ? read.reason : ''
    expect(reason).toMatch(/Split it into smaller files/)
    // The limit is in characters, so the message counts characters; the size on
    // disk is bytes, which for an accented file is the larger number.
    expect(reason).toContain('8,000,001 characters')
    expect(reason).toContain('over the 8,000,000')
  })

  it('never lets through a file the backend would refuse after the upload (#94)', () => {
    // What ties the two caps: a file that passes here must not be refused by
    // the backend once its whole body is sent. Lowering the backend's cap below
    // this one, or raising this one above it, fails here, not in production.
    for (const schema of ['ImportRunCreate', 'ImportInspectIn']) {
      expect(MAX_IMPORT_CHARACTERS).toBeLessThanOrEqual(backendCap(schema))
    }
  })

  it('accepts a file right on the limit', async () => {
    const file = new File(['a'.repeat(MAX_IMPORT_CHARACTERS)], 'big.csv')
    await expect(readCsvFile(file)).resolves.toMatchObject({ ok: true })
  })
})
