/**
 * The path a write takes when it does not land.
 *
 * This is the one place on the screen where being wrong is silent by
 * construction. The card is gone before the answer comes back, so a `POST` that
 * fails leaves nothing on screen that says so unless the banner does — and a
 * person finishing a fast run would count fifty triaged where forty-nine were.
 * So the banner is not cleared by the next keystroke, it names the contact, and
 * the decision it describes is taken off what `u` offers to take back.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'

import { currentName, renderTriage } from './test-render'

function press(key: string) {
  fireEvent.keyDown(window, { key })
}

/** Fails the decision on `contactIds`, and lets everything else through. */
function failDecisionsFor(contactIds: number[]) {
  return async (request: Request, next: (request: Request) => Promise<Response>) => {
    const { pathname } = new URL(request.url)
    if (request.method === 'POST' && pathname === '/api/v1/triage/decisions') {
      const body = (await request.clone().json()) as { contact_id: number }
      if (contactIds.includes(body.contact_id)) {
        return jsonResponse({ detail: 'the database is locked' }, 500)
      }
    }
    return next(request)
  }
}

/** Answers the decision on `contactId` the way the route answers one that left the queue. */
function leftTheQueue(contactId: number, body: Record<string, unknown>) {
  return async (request: Request, next: (request: Request) => Promise<Response>) => {
    const { pathname } = new URL(request.url)
    if (request.method === 'POST' && pathname === '/api/v1/triage/decisions') {
      const sent = (await request.clone().json()) as { contact_id: number }
      if (sent.contact_id === contactId) return jsonResponse(body, 409)
    }
    return next(request)
  }
}

describe('a decision that is not recorded', () => {
  it.each([
    [{ detail: 'archived' }, /Ada Example-1 was not recorded: they were archived after this/],
    [
      { detail: 'merged', merged_into_id: 3 },
      /Ada Example-1 was not recorded: they were merged into another contact after this/,
    ],
  ])('says in words why a contact that left the queue was refused (#222)', async (body, says) => {
    renderTriage({ contacts: 5, intercept: leftTheQueue(1, body) })
    await currentName()

    press('m')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(says)
    expect(alert).not.toHaveTextContent(/recorded: (archived|merged)\./)
  })

  it('names the contact it lost', async () => {
    const { backend } = renderTriage({ contacts: 5, intercept: failDecisionsFor([1]) })
    await currentName()

    press('m')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/Ada Example-1 was not recorded/)
    expect(backend.byId(1).met).toBe('unknown')
  })

  it('survives the rest of the run rather than being erased by the next key', async () => {
    const { backend } = renderTriage({ contacts: 6, intercept: failDecisionsFor([1]) })
    await currentName()

    press('m')
    await screen.findByRole('alert')

    // Four more decisions, all of which land.
    for (const contact of [2, 3, 4, 5]) {
      press('m')
      await waitFor(() => expect(backend.byId(contact).met).toBe('met'))
    }

    expect(screen.getByRole('alert')).toHaveTextContent(/Ada Example-1 was not recorded/)
  })

  it('accumulates, so a second loss does not hide the first', async () => {
    const { backend } = renderTriage({ contacts: 6, intercept: failDecisionsFor([1, 3]) })
    await currentName()

    press('m')
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/Ada Example-1/))
    press('m')
    await waitFor(() => expect(backend.byId(2).met).toBe('met'))
    press('m')

    const alert = await waitFor(() => {
      const found = screen.getByRole('alert')
      expect(found).toHaveTextContent(/Cleo Placeholder-3/)
      return found
    })
    expect(alert).toHaveTextContent(/Ada Example-1/)
    expect(alert).toHaveTextContent(/Dismiss all/)
  })

  it('goes away when it is dismissed, and not before', async () => {
    renderTriage({ contacts: 4, intercept: failDecisionsFor([1]) })
    await currentName()

    press('m')
    const alert = await screen.findByRole('alert')

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(alert).not.toBeInTheDocument())
  })

  it('is not something u offers to take back', async () => {
    const { backend } = renderTriage({ contacts: 4, intercept: failDecisionsFor([1]) })
    await currentName()

    press('m')
    await screen.findByRole('alert')

    // Nothing was written, so there is nothing to name.
    expect(screen.getByTestId('undo-affordance')).toHaveTextContent(
      'u takes back the newest triage decision.',
    )
    expect(screen.getByTestId('undo-affordance')).not.toHaveTextContent(/Ada Example-1/)

    // And the decision that does land afterwards is what u names.
    press('m')
    await waitFor(() => expect(backend.byId(2).met).toBe('met'))
    expect(screen.getByTestId('undo-affordance')).toHaveTextContent(/u takes back: met — Bo/)
  })
})
