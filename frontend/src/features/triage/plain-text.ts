/**
 * Message bodies are untrusted input (issue #75).
 *
 * A LinkedIn InMail body is raw HTML, and the archive stores it truncated, so a
 * summary can end mid-tag: `…read more <a href="https://exam`. React escapes
 * whatever it renders, so nothing here is about safety from injection —
 * `dangerouslySetInnerHTML` is never used on this screen and must never be. This
 * is about legibility: markup rendered verbatim as text is noise in an evidence
 * panel that has to be read in a couple of seconds.
 *
 * `DOMParser` builds an inert document: it runs no script, loads no image, and
 * attaches nothing to the page, and it closes a tag the input left open. Reading
 * `textContent` back off it is the whole transform. When it is unavailable or
 * throws, the raw string is returned and React escapes it, which is ugly but
 * never unsafe.
 */

/** True when the string carries something that looks like markup, open or truncated. */
function looksLikeMarkup(value: string): boolean {
  return /<[a-z!/]/i.test(value) || /&[a-z#][a-z0-9]{1,8};/i.test(value)
}

/**
 * The readable text of a message body, with markup and entities resolved away.
 *
 * Returns `null` for nothing worth showing, so a caller can skip the row.
 */
export function toPlainText(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null
  const raw = value.trim()
  if (raw === '') return null
  if (!looksLikeMarkup(raw)) return collapse(raw)

  try {
    const parsed = new DOMParser().parseFromString(raw, 'text/html')
    const text = parsed.body.textContent ?? ''
    // An input that is only a truncated opening tag parses to nothing; keeping
    // the raw text then says more than an empty panel does.
    return collapse(text) ?? collapse(raw)
  } catch {
    return collapse(raw)
  }
}

/** One space for any run of whitespace, so a pasted email body stays one block. */
function collapse(value: string): string | null {
  const text = value.replace(/\s+/g, ' ').trim()
  return text === '' ? null : text
}

/**
 * `text` cut to `limit` characters, on a word boundary when one falls in the back
 * half of the cut, with an ellipsis.
 */
export function truncate(text: string, limit: number): string {
  if (text.length <= limit) return text
  const cut = text.slice(0, limit)
  const space = cut.lastIndexOf(' ')
  return `${(space > limit * 0.5 ? cut.slice(0, space) : cut).trimEnd()}…`
}
