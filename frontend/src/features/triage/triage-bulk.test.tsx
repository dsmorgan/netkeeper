/**
 * The bulk suggestion banner (spec 10.2).
 *
 * The count on the banner is a preview, and the apply sends it back. When the
 * set has moved the API refuses with `409` and writes nothing — which is not a
 * failure to report but a preview to take again, and that is what this asserts.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { currentName, renderTriage } from './test-render'

describe('the bulk suggestion', () => {
  it('is not drawn when it matches nobody', async () => {
    renderTriage({ contacts: 4, withMessages: 0 })
    await currentName()

    await waitFor(() => expect(screen.queryByTestId('bulk-suggestion')).not.toBeInTheDocument())
  })

  it('previews with a count and applies as one batch', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()

    const banner = await screen.findByTestId('bulk-suggestion')
    expect(banner).toHaveTextContent('You have message threads with 3 untriaged people.')

    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(backend.byId(2).met).toBe('met')
    expect(backend.byId(3).met).toBe('met')
    expect(backend.byId(4).met).toBe('unknown')
    expect(await screen.findByRole('status')).toHaveTextContent(/Marked 3 contacts as met/)
    // One batch id, so one undo takes it all back.
    const batches = new Set(backend.decisions.map((decision) => decision.batch_id))
    expect(batches.size).toBe(1)
  })

  it('goes away once everyone it matched has an answer', async () => {
    renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))

    await waitFor(() => expect(screen.queryByTestId('bulk-suggestion')).not.toBeInTheDocument())
  })

  it('re-previews instead of erroring when the count moved', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await screen.findByRole('button', { name: 'Mark 3 as met' })

    // A fourth person gains message history after the banner was drawn.
    backend.setMessageCount(4, 2)
    fireEvent.click(screen.getByRole('button', { name: 'Mark 3 as met' }))

    // Nothing was applied, and the banner comes back with the count as it is.
    expect(await screen.findByRole('button', { name: 'Mark 4 as met' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/nothing was applied/i)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    for (const id of [1, 2, 3, 4]) expect(backend.byId(id).met).toBe('unknown')

    // Accepting the new count applies it.
    fireEvent.click(screen.getByRole('button', { name: 'Mark 4 as met' }))
    await waitFor(() => expect(backend.byId(4).met).toBe('met'))
  })

  it('is taken back whole by one undo', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))
    await waitFor(() => expect(backend.byId(3).met).toBe('met'))

    fireEvent.keyDown(window, { key: 'u' })

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    expect(backend.byId(2).met).toBe('unknown')
    expect(backend.byId(3).met).toBe('unknown')
    expect(await screen.findByTestId('bulk-suggestion')).toHaveTextContent('3 untriaged people')
  })

  it('moves the queue on when the batch empties the front of it', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    expect(await currentName()).toContain('Ada')

    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))

    await waitFor(() => expect(backend.byId(3).met).toBe('met'))
    await waitFor(async () => expect(await currentName()).toContain('Dev'))
  })
})

describe('what netkeeper decided (P1-28)', () => {
  /** Accepts the offered batch, which is what leaves decisions to review. */
  async function acceptTheBatch() {
    fireEvent.click(await screen.findByRole('button', { name: /^Mark \d+ as met$/ }))
    await screen.findByRole('status')
  }

  it('says how many are waiting, and opens the queue that walks them', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await acceptTheBatch()

    const prompt = await screen.findByTestId('automatic-pass')
    expect(prompt).toHaveTextContent('netkeeper decided 3 contacts from a batch you accepted')

    // An answer of the person's own, in a state the review queue also serves:
    // it must stay out of it, which is the whole point of `decided_by`.
    fireEvent.keyDown(window, { key: 'n' })
    await waitFor(() => expect(backend.byId(4).met).toBe('not_met'))
    expect(backend.byId(4).met_source).toBe('manual')

    fireEvent.click(screen.getByRole('button', { name: 'Review them' }))

    // The queue now serves the contacts the batch decided, not the untriaged
    // and not the one answered by hand.
    await waitFor(async () => expect(await currentName()).toContain('Ada'))
    expect(backend.byId(1).met).toBe('met')
    const ahead = screen.getByTestId('triage-queue-list')
    expect(ahead).not.toHaveTextContent('Dev Testerly-4')
    expect(screen.getByRole('button', { name: 'Reviewing' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByTestId('automatic-pass')).toHaveTextContent(
      'are the 3 contacts netkeeper decided for you, waiting to be checked',
    )
  })

  it('offers no batch while reviewing, because a batch never reaches an answer', async () => {
    // The service refuses `met`/`not_met` outright (422), so asking at all
    // would be a request this screen knows better than to send.
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await acceptTheBatch()
    fireEvent.click(await screen.findByRole('button', { name: 'Review them' }))
    await waitFor(async () => expect(await currentName()).toContain('Ada'))

    expect(screen.queryByTestId('bulk-suggestion')).not.toBeInTheDocument()
    const asked = backend.seen.filter(
      (request) =>
        request.path === '/api/v1/triage/suggestions' &&
        request.search.getAll('states').includes('met'),
    )
    expect(asked).toEqual([])
  })

  it('takes a contact out of the review queue when you answer it yourself', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await acceptTheBatch()
    fireEvent.click(await screen.findByRole('button', { name: 'Review them' }))
    await waitFor(async () => expect(await currentName()).toContain('Ada'))

    // `m` on the card in front: the same answer, now the person's own.
    fireEvent.keyDown(window, { key: 'm' })
    await waitFor(() => expect(backend.byId(1).met_source).toBe('manual'))
    expect(backend.byId(1).met).toBe('met')
    await waitFor(() =>
      expect(screen.getByTestId('automatic-pass')).toHaveTextContent(
        'are the 2 contacts netkeeper decided for you',
      ),
    )
    // And the queue moved on to the next one still waiting to be checked.
    expect(await currentName()).toContain('Bo')
  })

  it('says nothing at all until a batch has been accepted', async () => {
    renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await screen.findByTestId('bulk-suggestion')

    expect(screen.queryByTestId('automatic-pass')).not.toBeInTheDocument()
  })

  it('names the contacts a batch covers before it is applied', async () => {
    renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await screen.findByTestId('bulk-suggestion')

    fireEvent.click(screen.getByRole('button', { name: 'See who' }))

    const names = await screen.findByTestId('suggestion-contacts')
    expect(names).toHaveTextContent('Ada Example-1')
    expect(names).toHaveTextContent('Cleo Placeholder-3')
    expect(names).not.toHaveTextContent('Dev Testerly-4')
  })
})
