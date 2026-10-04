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

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { contactDetail } from '@/test/contacts-fixtures'
import { jsonResponse } from '@/test/fetch'

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

describe('a possible duplicate of a card contact (#363)', () => {
  const kept = contactDetail({
    id: 2,
    first_name: 'Ada',
    last_name: 'Ventura',
    preferred_name: 'Ada',
  })
  const moves = {
    emails: { moved: 0, dropped: 0 },
    phones: { moved: 0, dropped: 0 },
    links: { moved: 0, dropped: 0 },
    positions: { moved: 0, dropped: 0 },
    snapshots: 0,
    interactions: 0,
    tags_added: [],
    tags_removed: [],
    lists_added: [],
    enrollments_moved: 0,
    enrollments_combined: 0,
    messages_moved: 0,
    messages_discarded: 0,
    history_rows: 0,
  }

  function renderWithDuplicate() {
    const backend = backendWithACard()
    const posted: string[] = []
    const utils = renderTriage({
      backend,
      intercept: async (request, next) => {
        const { pathname } = new URL(request.url)
        if (pathname === '/api/v1/contacts/1/duplicates') {
          return jsonResponse([
            {
              contact_id: 2,
              first_name: 'Ada',
              last_name: 'Ventura',
              preferred_name: 'Ada',
              current_title: null,
              current_company: null,
              li_public_id: 'ada-ventura',
              needs_review: false,
              matched_by: ['name'],
              linkedin_ids_differ: false,
            },
          ])
        }
        if (pathname === '/api/v1/contacts/2/merge/preview') {
          posted.push(pathname)
          const loser = contactDetail({
            id: 1,
            first_name: 'Bo',
            last_name: 'Example',
            preferred_name: 'Bo',
            needs_review_at: '2026-09-24T12:00:00Z',
          })
          return jsonResponse({ survivor: kept, loser, result: kept, moves, undoable: false })
        }
        if (pathname === '/api/v1/contacts/2/merge') {
          posted.push(pathname)
          backend.mergeAway(1, 2)
          return jsonResponse(kept)
        }
        return next(request)
      },
    })
    return { ...utils, posted }
  }

  it('shows the hint in the review band and merges only after the confirmation', async () => {
    const { backend, posted } = renderWithDuplicate()
    const name = await currentName()

    const band = screen.getByRole('region', { name: 'Needs review' })
    const hint = await within(band).findByRole('list', { name: 'Possible duplicates' })
    expect(hint).toHaveTextContent('Possible duplicate of Ada Ventura')
    // The other contact opens in a new tab, so the run keeps its place.
    expect(within(hint).getByRole('link', { name: 'Ada Ventura' })).toHaveAttribute(
      'target',
      '_blank',
    )

    fireEvent.click(within(hint).getByRole('button', { name: 'Merge with Ada Ventura' }))
    const panel = await screen.findByRole('region', { name: 'Merge contacts' })
    await within(panel).findByRole('columnheader', { name: 'Stays: Ada Ventura' })
    // The heading names the pair exactly as the table and the buttons do.
    const away = within(panel).getByRole('columnheader', { name: /^Merged away: / })
    const fold = (away.textContent ?? '').replace('Merged away: ', '')
    expect(within(panel).getByRole('heading', { level: 3 })).toHaveTextContent(
      `Merge ${fold} with Ada Ventura`,
    )
    expect(within(panel).getByRole('button', { name: `Keep ${fold} instead` })).toBeVisible()

    // While the panel is open, the keyboard map stands aside: no key decides the card.
    fireEvent.keyDown(window, { key: 'm' })
    expect(backend.countOf('/api/v1/triage/decisions')).toBe(0)

    fireEvent.click(within(panel).getByRole('button', { name: 'Merge…' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/A merge can.t be undone/)
    expect(posted).not.toContain('/api/v1/contacts/2/merge')

    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Merge' }))
      await Promise.resolve()
    })
    await waitFor(() =>
      expect(screen.getByTestId('triage-notice')).toHaveTextContent(/Merged, and moved on/),
    )
    expect(posted.filter((path) => path === '/api/v1/contacts/2/merge')).toHaveLength(1)
    expect(screen.queryByRole('region', { name: 'Merge contacts' })).toBeNull()
    // The card merged away is behind: the next contact is on screen, with nothing decided.
    await waitFor(async () => expect(await currentName()).not.toBe(name))
    expect(screen.queryByRole('region', { name: 'Needs review' })).toBeNull()
    expect(backend.byId(1).met).toBe('unknown')
    expect(backend.countOf('/api/v1/triage/decisions')).toBe(0)
  })

  it('shows no hint for an ordinary contact', async () => {
    const backend = backendWithACard()
    backend.diverge(1, { needs_review_at: null })
    renderTriage({ backend })
    await currentName()
    expect(screen.queryByRole('list', { name: 'Possible duplicates' })).toBeNull()
    expect(backend.seen.some((entry) => entry.path.endsWith('/duplicates'))).toBe(false)
  })
})
