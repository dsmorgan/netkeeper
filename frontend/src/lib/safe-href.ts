/**
 * The href a stored link may be rendered with: its url when that is an absolute http or
 * https url, else null, and the caller shows the url as plain text (#206 review). A link
 * reaches the page from a source the page does not control -- an import, a harvested
 * website -- and `javascript:` or `data:` in an href would run or show something rather
 * than open a site. `URL` reads the scheme the way the browser will: after trimming,
 * with tabs and newlines removed.
 */
export function safeHref(url: string): string | null {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return null
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return null
  }
  return parsed.hostname ? parsed.href : null
}
