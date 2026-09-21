/**
 * Going back, which is not undo (issue #114).
 *
 * "You can go forward in triage, but not backwards. That's not a great
 * experience and undo isn't the answer to that as undo should change tagging."
 * So the two are separate things and this file holds the line between them:
 *
 * - `←` is a **cursor**. It sends no request, changes nothing on the server, and
 *   leaves the undo stack exactly where it was.
 * - `u` is a **write**, and it takes back the newest one, whatever card is on
 *   screen. Pressing it while looking back at somebody else proves it.
 *
 * Deciding from the trail is the third thing: a real write, on the contact you
 * walked back to, which the server logs as another decision so undo still walks
 * back one at a time.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'

import { currentName, renderTriage } from './test-render'

function press(key: string) {
  fireEvent.keyDown(window, { key })
}

describe('stepping back through the run', () => {
  it('goes back to the contact just decided and writes nothing to get there', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    expect(await currentName()).toContain('Ada')

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(await currentName()).toContain('Bo')
    const requests = backend.seen.length

    press('ArrowLeft')

    expect(await currentName()).toContain('Ada')
    // A cursor, not a request: nothing went out and nothing changed.
    expect(backend.seen).toHaveLength(requests)
    expect(backend.byId(1).met).toBe('met')
    expect(backend.decisions).toHaveLength(1)
    expect(backend.decisions[0]?.undone_at).toBeNull()
  })

  it('shows the decision the contact already carries', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('n')
    await waitFor(() => expect(backend.byId(1).met).toBe('not_met'))
    press('ArrowLeft')

    const review = await screen.findByTestId('card-review')
    expect(review).toHaveTextContent(/Not met/)
    expect(review).toHaveTextContent(/wrote nothing/i)
    expect(screen.getByTestId('triage-notice')).toHaveTextContent(/Going back wrote nothing/i)
  })

  it('says where you are, in the run and in the queue', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()
    expect(screen.getByTestId('card-position')).toHaveTextContent('Contact 1 of this run')
    expect(screen.getByTestId('card-position')).toHaveTextContent('5 left in this queue')

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(screen.getByTestId('card-position')).toHaveTextContent('Contact 2 of this run')
    expect(screen.getByTestId('card-position')).toHaveTextContent('4 left in this queue')

    press('ArrowLeft')
    expect(screen.getByTestId('card-position')).toHaveTextContent(
      'Looking back: 1 of the 1 you have already seen',
    )
  })

  it('changes a decision from the trail and returns to where you were', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(await currentName()).toContain('Bo')

    press('ArrowLeft')
    expect(await currentName()).toContain('Ada')
    press('n')

    await waitFor(() => expect(backend.byId(1).met).toBe('not_met'))
    // Two decision rows on the same contact, which is how the service records a
    // change of mind — so undo still walks back one decision at a time.
    const hers = backend.decisions.filter((decision) => decision.contact_id === 1)
    expect(hers).toHaveLength(2)
    expect(hers[1]?.before_state.met).toBe('met')
    expect(hers[1]?.after_state.met).toBe('not_met')
    // And the cursor stepped forward, back to the card the run was on.
    expect(await currentName()).toContain('Bo')
    expect(backend.byId(2).met).toBe('unknown')
  })

  it('keeps the counters honest when a decision only replaces another', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('1 / 5')

    press('ArrowLeft')
    press('n')

    await waitFor(() => expect(backend.byId(1).met).toBe('not_met'))
    // Still one contact triaged, not two: the same person changed their answer.
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('1 / 5')
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('4 left in this queue')
  })

  it('leaves the undo stack alone, so u still takes back the newest write', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('m') // Ada
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('m') // Bo
    await waitFor(() => expect(backend.byId(2).met).toBe('met'))

    press('ArrowLeft')
    press('ArrowLeft')
    expect(await currentName()).toContain('Ada')
    // The affordance names the write, not the card under the cursor.
    expect(screen.getByTestId('undo-affordance')).toHaveTextContent(/u takes back: met — Bo/)

    press('u')

    // Undo took back the newest write — Bo — although Ada is the one on screen.
    await waitFor(() => expect(backend.byId(2).met).toBe('unknown'))
    expect(backend.byId(1).met).toBe('met')
    expect(screen.getByTestId('triage-notice')).toHaveTextContent(/Undo took back/i)
  })

  it('walks forward again, and → returns to the live card', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('m')
    await waitFor(() => expect(backend.byId(2).met).toBe('met'))
    expect(await currentName()).toContain('Cleo')

    press('ArrowLeft')
    press('ArrowLeft')
    expect(await currentName()).toContain('Ada')

    press('ArrowRight')
    expect(await currentName()).toContain('Bo')

    press('ArrowRight')
    expect(await currentName()).toContain('Cleo')
    expect(screen.getByTestId('triage-notice')).toHaveTextContent(/Back at the queue, on Cleo/)
    // Walking the trail never asked the server for anything.
    expect(backend.countOf('/api/v1/triage/next', 'GET')).toBe(1)
  })

  it('says so at either end of the trail rather than doing nothing', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('ArrowLeft')
    expect(screen.getByTestId('triage-notice')).toHaveTextContent(
      /Nothing behind you: this is the first contact of the run/i,
    )
    expect(await currentName()).toContain('Ada')

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('ArrowLeft')
    press('ArrowLeft')

    expect(screen.getByTestId('triage-notice')).toHaveTextContent(/as far back as this run goes/i)
    expect(await currentName()).toContain('Ada')
  })

  it('undoes a change of mind back to the decision before it, not past it', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('ArrowLeft')
    press('n')
    await waitFor(() => expect(backend.byId(1).met).toBe('not_met'))

    press('u')

    // One decision at a time: back to met, which is what the service logs a
    // change of mind as, rather than all the way to untriaged.
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))

    press('u')
    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    expect(await currentName()).toContain('Ada')
  })

  it('goes back over the run once the queue is empty', async () => {
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('m')
    await waitFor(() => expect(backend.byId(2).met).toBe('met'))
    await screen.findByText('Nothing left to triage.')

    fireEvent.click(screen.getByRole('button', { name: 'Go back over this run' }))

    expect(await currentName()).toContain('Bo')
    expect(screen.queryByText('Nothing left to triage.')).not.toBeInTheDocument()
  })

  it('closes the name editor when the cursor steps back, like any card change', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('p')
    await screen.findByLabelText('Preferred name')

    press('ArrowLeft')

    // The editor belonged to Bo; it is not sitting over Ada holding her name.
    expect(await currentName()).toContain('Ada')
    expect(screen.queryByLabelText('Preferred name')).not.toBeInTheDocument()
  })
})

describe('the queue list', () => {
  it('shows what is passed, what is on screen, and what is next', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('ArrowRight')
    await waitFor(async () => expect(await currentName()).toContain('Cleo'))

    const list = await screen.findByTestId('triage-queue-list')
    expect(within(list).getByRole('button', { name: /Ada Example-1 — Met/ })).toBeInTheDocument()
    expect(
      within(list).getByRole('button', { name: /Bo Sample-2 — Passed over/ }),
    ).toBeInTheDocument()
    expect(list).toHaveTextContent(/Cleo Placeholder-3/)
    expect(list).toHaveTextContent(/On screen/)
  })

  it('opens a contact from the list without writing anything', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('m')
    await waitFor(() => expect(backend.byId(2).met).toBe('met'))
    const requests = backend.seen.length

    const list = screen.getByTestId('triage-queue-list')
    fireEvent.click(within(list).getByRole('button', { name: /Ada Example-1 — Met/ }))

    expect(await currentName()).toContain('Ada')
    expect(backend.seen).toHaveLength(requests)
    expect(backend.decisions.filter((decision) => decision.undone_at !== null)).toHaveLength(0)
  })

  it('offers the way back to the live card while you are looking back', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('ArrowLeft')
    expect(await currentName()).toContain('Ada')

    const list = screen.getByTestId('triage-queue-list')
    fireEvent.click(within(list).getByRole('button', { name: /Bo Sample-2 — Where you were/ }))

    expect(await currentName()).toContain('Bo')
  })
})

describe('a decision that was refused', () => {
  it('stays reachable, and the banner says the contact is still untriaged', async () => {
    // The first write for Ada is refused; the second one, from the trail, lands.
    let refusals = 0
    const { backend } = renderTriage({
      contacts: 5,
      intercept: async (request, next) => {
        const { pathname } = new URL(request.url)
        if (request.method === 'POST' && pathname === '/api/v1/triage/decisions') {
          const body = (await request.clone().json()) as { contact_id: number }
          if (body.contact_id === 1 && refusals === 0) {
            refusals += 1
            return jsonResponse({ detail: 'the database is locked' }, 500)
          }
        }
        return next(request)
      },
    })
    await currentName()

    press('m')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/They stay untriaged and come back the next time/i)
    expect(alert).toHaveTextContent(/goes back to them in this run/i)

    // The trail marks them, and going back offers another go at it.
    const list = screen.getByTestId('triage-queue-list')
    const row = within(list).getByRole('button', { name: /Ada Example-1 — Not recorded/ })
    fireEvent.click(row)
    expect(await currentName()).toContain('Ada')

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
  })
})
