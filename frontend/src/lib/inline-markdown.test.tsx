import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderInlineMarkdown } from './inline-markdown'

function renderText(text: string) {
  render(<p>{renderInlineMarkdown(text)}</p>)
}

describe('renderInlineMarkdown', () => {
  it('renders plain text with no markers unchanged', () => {
    renderText('nothing special here')
    expect(screen.getByText('nothing special here')).toBeInTheDocument()
  })

  it('renders **bold** as <strong>, not literal asterisks', () => {
    renderText('this report reads configuration and counters, **never callers**.')
    const strong = screen.getByText('never callers')
    expect(strong.tagName).toBe('STRONG')
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument()
  })

  it('renders *italic* as <em>, not literal asterisks, and does not confuse it with bold', () => {
    renderText('this reports the *stored* due time for each job kind.')
    const em = screen.getByText('stored')
    expect(em.tagName).toBe('EM')
    expect(screen.queryByText(/\*/)).not.toBeInTheDocument()
  })

  it('renders `code` as <code>, not literal backticks', () => {
    renderText('log in, then run `netkeeper preflight`.')
    const code = screen.getByText('netkeeper preflight')
    expect(code.tagName).toBe('CODE')
    expect(screen.queryByText(/`/)).not.toBeInTheDocument()
  })

  it('renders several markers in the same sentence, in order', () => {
    renderText('run `netkeeper serve`, which is **wired** and *live*.')
    expect(screen.getByText('netkeeper serve').tagName).toBe('CODE')
    expect(screen.getByText('wired').tagName).toBe('STRONG')
    expect(screen.getByText('live').tagName).toBe('EM')
  })

  it('never produces raw HTML: an angle-bracket source stays inert text, not an element', () => {
    renderText('a `<script>` tag stays literal text inside the code span')
    // If this were ever rendered via dangerouslySetInnerHTML, this would be a
    // real <script> element in the DOM instead of text inside <code>.
    expect(document.querySelector('script')).toBeNull()
    expect(screen.getByText('<script>').tagName).toBe('CODE')
  })
})
