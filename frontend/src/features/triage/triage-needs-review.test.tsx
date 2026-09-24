/**
 * A contact read off a connections-page card, on the triage screen (#184).
 *
 * The card says so on its State line, and the confirm and reject answers lead
 * the column beside the card (under it on a narrow screen), where they move
 * nothing above the decision row (spec 10.2's layout rules;
 * jsdom has no layout, so the suite holds the structure and a real browser
 * measures the rest). Neither answer is a triage decision: met, not met, and
 * skip still work on the card, and undo never reaches a confirm or a reject.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { createFakeBackend } from './test-backend'
import { currentName, renderTriage } from './test-render'

function backendWithACard() {
  const backend = createFakeBackend({ contacts: 3 })
  backend.diverge(1, { needs_review_at: '2026-09-24T12:00:00Z' })
  return backend
}

describe('a contact read off a connections-page card', () => {
  it('carries a badge on the State line and the notice after the card', async () => {
    renderTriage({ backend: backendWithACard() })
    await currentName()

    const card = screen.getByTestId('triage-card')
    expect(within(card).getByTestId('needs-review-badge')).toHaveTextContent('Needs review')
    const notice = screen.getByRole('region', { name: 'Needs review' })
    // After the card and its decision row in the document, never between them:
    // beside the card at lg, under it below that.
    expect(card.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const decision = screen.getByRole('button', { name: /^Met/ })
    expect(decision.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows neither for an ordinary contact', async () => {
    const backend = backendWithACard()
    backend.diverge(1, { needs_review_at: null })
    renderTriage({ backend })
    await currentName()

    expect(screen.queryByTestId('needs-review-badge')).toBeNull()
    expect(screen.queryByRole('region', { name: 'Needs review' })).toBeNull()
  })

  it('confirms: the mark goes, the card stays, and nothing is decided', async () => {
    const { backend } = renderTriage({ backend: backendWithACard() })
    const name = await currentName()

    fireEvent.click(screen.getByTestId('needs-review-confirm'))

    await waitFor(() => expect(backend.byId(1).needs_review_at).toBeNull())
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Needs review' })).toBeNull())
    expect(screen.queryByTestId('needs-review-badge')).toBeNull()
    expect(await currentName()).toBe(name)
    expect(backend.byId(1).met).toBe('unknown')
    expect(screen.getByTestId('triage-notice')).toHaveTextContent(/Confirmed/)
    expect(screen.getByTestId('undo-affordance')).not.toHaveTextContent(/Confirm/)
  })

  it('rejects: the contact is archived, never deleted, and says how to move on', async () => {
    const { backend } = renderTriage({ backend: backendWithACard() })
    await currentName()

    fireEvent.click(screen.getByTestId('needs-review-reject'))

    await waitFor(() => expect(backend.byId(1).archived_at).not.toBeNull())
    expect(backend.byId(1).needs_review_at).not.toBeNull()
    expect(backend.contacts).toHaveLength(3)
    expect(await screen.findByText(/Rejected: it.s archived, not deleted/)).toBeInTheDocument()
    expect(screen.getByTestId('triage-notice')).toHaveTextContent(/leaves the queue/)
    expect(screen.queryByTestId('needs-review-reject')).toBeNull()

    fireEvent.keyDown(window, { key: 'ArrowRight' })
    await waitFor(async () => expect(await currentName()).not.toContain(backend.byId(1).first_name))
  })

  it('says so when the write fails, and changes nothing on the card', async () => {
    const backend = backendWithACard()
    renderTriage({
      backend,
      intercept: (request, next) =>
        new URL(request.url).pathname.endsWith('/confirm')
          ? Promise.resolve(new Response(JSON.stringify({ detail: 'locked' }), { status: 500 }))
          : next(request),
    })
    await currentName()

    fireEvent.click(screen.getByTestId('needs-review-confirm'))

    expect(await screen.findByRole('alert')).toHaveTextContent(/locked/)
    expect(screen.getByRole('region', { name: 'Needs review' })).toBeInTheDocument()
    expect(backend.byId(1).needs_review_at).not.toBeNull()
  })
})
