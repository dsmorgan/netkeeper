import type { ReactNode } from 'react'

/** Backtick code, then **bold**, then *italic* — order matters: at the start of
 *  "**x**", the bold alternative has to be tried before the italic one, or a
 *  single leading `*` would match first and split the pair in half. */
const TOKEN = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*/g

/**
 * A tiny, safe subset of Markdown — backtick code, `**bold**`, and `*italic*`
 * — rendered as React nodes, never raw HTML: every span this returns is a
 * plain string or a `<code>`/`<strong>`/`<em>` wrapping one, so nothing here
 * ever reaches `dangerouslySetInnerHTML` and nothing in the source text can
 * inject an element React did not itself create.
 *
 * `netkeeper/services/posture.py`'s `GAPS` and every protection's `warnings`
 * and `value` are written in exactly this subset for the terminal report
 * (`netkeeper posture`; `render()`'s `_wrapped` just wraps the text, so the
 * backticks and asterisks print as themselves there) — this renders the same
 * three markers instead of showing them literally, which is what a person
 * reading the Settings page's Posture card sees otherwise (review179r2).
 */
export function renderInlineMarkdown(text: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let lastIndex = 0
  let key = 0
  for (const match of text.matchAll(TOKEN)) {
    const index = match.index
    if (index > lastIndex) nodes.push(text.slice(lastIndex, index))
    const [whole, code, bold, italic] = match
    if (code !== undefined) {
      nodes.push(
        <code key={key} className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
          {code}
        </code>,
      )
    } else if (bold !== undefined) {
      nodes.push(
        <strong key={key} className="font-medium">
          {bold}
        </strong>,
      )
    } else {
      nodes.push(<em key={key}>{italic}</em>)
    }
    key += 1
    lastIndex = index + whole.length
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex))
  return nodes
}
