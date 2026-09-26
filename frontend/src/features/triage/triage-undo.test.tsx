/**
 * Undo: exact, refusable, and honest about what it will take back.
 *
 * The refusal is the interesting half. The API answers `409` and writes nothing
 * when the contact no longer holds what the decision left it holding — an edit
 * from elsewhere, or a contact archived or merged away since. The screen has to
 * put that to the person as a choice, not swallow it and not force it.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'

import { currentName, renderTriage } from './test-render'

function press(key: string) {
  fireEvent.keyDown(window, { key })
}

describe('undo', () => {
  it('takes back a met decision and puts the contact back in front of you', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(await currentName()).toContain('Bo')

    press('u')

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    expect(backend.byId(1).triaged_at).toBeNull()
    expect(await currentName()).toContain('Ada')
  })

  it.each([
    ['m', 'met'],
    ['n', 'not_met'],
    ['s', 'skip'],
  ])('takes back a %s decision', async (key, expected) => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press(key)
    await waitFor(() => expect(backend.byId(1).met).toBe(expected))

    press('u')
    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
  })

  it('takes back a preferred-name edit without losing the card', async () => {
    const { backend } = renderTriage({ contacts: 3 })
    await currentName()

    press('p')
    const field = await screen.findByLabelText('Preferred name')
    fireEvent.change(field, { target: { value: 'Addie' } })
    fireEvent.submit(field.closest('form')!)
    await waitFor(() => expect(backend.byId(1).preferred_name).toBe('Addie'))

    press('u')

    await waitFor(() => expect(backend.byId(1).preferred_name).toBe('Ada'))
    expect(await currentName()).toContain('Ada')
    // One card, not two: the restored contact replaced the head rather than
    // being pushed in front of itself.
    expect(screen.getAllByTestId('triage-card')).toHaveLength(1)
  })

  it('walks back one decision at a time', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('n')
    await waitFor(() => expect(backend.byId(2).met).toBe('not_met'))

    press('u')
    await waitFor(() => expect(backend.byId(2).met).toBe('unknown'))
    expect(backend.byId(1).met).toBe('met')

    press('u')
    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
  })

  it('offers the 409 as a choice and writes nothing until it is taken', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    // Something else moved the contact on after the decision.
    backend.diverge(1, { met: 'not_met' })

    press('u')

    const prompt = await screen.findByRole('alertdialog')
    expect(prompt).toHaveTextContent(/changed since you decided/i)
    expect(prompt).toHaveTextContent(/undo would overwrite that change/i)
    expect(prompt).toHaveAttribute('aria-modal', 'false')
    // Refused, so nothing was written and nothing was forced on its own.
    expect(backend.byId(1).met).toBe('not_met')
    expect(backend.decisions[0]?.undone_at).toBeNull()
  })

  it('forces the undo only when the person asks for it', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    backend.diverge(1, { met: 'not_met' })

    press('u')
    fireEvent.click(await screen.findByTestId('undo-force'))

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
  })

  it('leaves the contact alone when the refusal is declined', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    backend.diverge(1, { met: 'not_met' })

    press('u')
    fireEvent.click(await screen.findByRole('button', { name: 'Leave it as it is' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(backend.byId(1).met).toBe('not_met')
  })

  it('words the refusal for a contact merged away since the decision', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    backend.mergeAway(1, 3)

    press('u')

    const prompt = await screen.findByRole('alertdialog')
    expect(prompt).toHaveTextContent(/merged into another one/i)
    expect(prompt).toHaveTextContent(/the surviving contact carries this decision now/i)
    expect(prompt).not.toHaveTextContent(/changed since you decided\. Undo anyway/i)
    expect(backend.byId(1).met).toBe('met')
  })

  it('words the refusal for a contact archived since the decision', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    backend.archive(1)

    press('u')

    const prompt = await screen.findByRole('alertdialog')
    expect(prompt).toHaveTextContent(/was archived since you decided/i)
    expect(prompt).toHaveTextContent(/does not come back into the queue/i)
    expect(backend.byId(1).met).toBe('met')
  })

  it('forces an archived contact back without pretending it is in the queue', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    backend.archive(1)
    press('u')
    fireEvent.click(await screen.findByTestId('undo-force'))

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    // Restored, but an archived contact is not one the queue serves, so it is
    // reported rather than put in front of the person as the next card.
    expect(await screen.findByText(/archived or merged away since/i)).toBeInTheDocument()
    expect(await currentName()).toContain('Bo')
  })

  it('keeps the card when a force only overrode an edited field', async () => {
    // `forced` is appended to for any divergence the force overrode, an
    // ordinary field edit included — which is the common case, since an archive
    // or a merge needs another actor. Reading it as "this contact left the
    // queue" would drop a card that is still in the queue and still on screen.
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('p')
    const field = await screen.findByLabelText('Preferred name')
    fireEvent.change(field, { target: { value: 'Addie' } })
    fireEvent.submit(field.closest('form')!)
    await waitFor(() => expect(backend.byId(1).preferred_name).toBe('Addie'))

    // Something else renames them behind the screen's back.
    backend.diverge(1, { preferred_name: 'Adelaide' })
    press('u')

    const prompt = await screen.findByRole('alertdialog')
    expect(prompt).toHaveTextContent(/changed since you decided/i)
    fireEvent.click(await screen.findByTestId('undo-force'))

    await waitFor(() => expect(backend.byId(1).preferred_name).toBe('Ada'))
    // Still the card, and showing what the server now holds.
    expect(await currentName()).toBe('Ada Example-1')
    expect(screen.queryByText(/not in this queue/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/archived or merged away/i)).not.toBeInTheDocument()
  })

  it('reads liveness off the card, not out of the refusal it forced past (#91)', async () => {
    // The case the old inference got wrong, and the reason `TriageContactOut`
    // carries `archived_at` and `merged_into_id` now.
    //
    // The `409` names an edited field, so the screen used to conclude "an
    // ordinary edit" and put the restored contact back at the front of the
    // queue. But a force skips every check, and the contact was archived while
    // the prompt was on screen: what came back is a contact the queue will
    // never serve again, presented as the next card. `forced` cannot tell the
    // two apart — it lists this contact in both cases — and the card can.
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    backend.diverge(1, { met: 'not_met' })
    press('u')
    const prompt = await screen.findByRole('alertdialog')
    expect(prompt).toHaveTextContent(/changed since you decided/i)

    // Somebody archives them between the refusal and the force.
    backend.archive(1)
    fireEvent.click(await screen.findByTestId('undo-force'))

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    expect(await screen.findByText(/archived or merged away since/i)).toBeInTheDocument()
    // And they are not the card: the queue does not serve an archived contact.
    expect(await currentName()).toContain('Bo')
  })

  it('does not reach past a decision whose write never landed', async () => {
    // `u` pressed underneath a decision that is still in flight means "take
    // back that decision". If it never reached the server's stack, sending the
    // undo anyway would revert the contact before it — silently, because both
    // sit behind the frontier and neither is served again.
    const { backend } = renderTriage({
      contacts: 5,
      latencyMs: 30,
      intercept: async (request, next) => {
        const { pathname } = new URL(request.url)
        if (request.method === 'POST' && pathname === '/api/v1/triage/decisions') {
          const body = (await request.clone().json()) as { contact_id: number }
          if (body.contact_id === 2) return jsonResponse({ detail: 'the database is locked' }, 500)
        }
        return next(request)
      },
    })
    await currentName()

    press('m') // contact 1, lands
    press('n') // contact 2, fails
    press('u') // pressed while contact 2's write is still in flight

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    await screen.findByText(/never reached the server/i)
    // The decision that did land is untouched, and nothing was undone.
    expect(backend.byId(1).met).toBe('met')
    expect(backend.decisions.filter((decision) => decision.undone_at !== null)).toHaveLength(0)
  })

  it('offers no force when another undo took the decision back first (#222)', async () => {
    // Another tab's undo reaches the server first and spends the decision; this
    // one loses the race. Forcing it would take back the decision *before*.
    let raced = false
    const { backend } = renderTriage({
      contacts: 4,
      intercept: async (request, next) => {
        const { pathname } = new URL(request.url)
        if (request.method === 'POST' && pathname === '/api/v1/triage/undo' && !raced) {
          raced = true
          const newest = backend.decisions.at(-1)
          await next(request) // the other tab's undo, which won
          return jsonResponse(
            {
              detail: `triage decision ${newest?.id} was undone by another request first; nothing changed`,
              reason: 'raced',
              decision_id: newest?.id,
            },
            409,
          )
        }
        return next(request)
      },
    })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('n')
    await waitFor(() => expect(backend.byId(2).met).toBe('not_met'))

    press('u')

    expect(await screen.findByTestId('triage-notice')).toHaveTextContent(
      /another undo took back .* first, so this one changed nothing/i,
    )
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(screen.queryByTestId('undo-force')).not.toBeInTheDocument()
    expect(backend.countOf('/api/v1/triage/undo', 'POST')).toBe(1)
    // The winner's undo, and nothing past it: the decision before is untouched.
    expect(backend.byId(2).met).toBe('unknown')
    expect(backend.byId(1).met).toBe('met')
    // The queue was read again, so the card is the contact the winner put back.
    await waitFor(async () => expect(await currentName()).toContain(backend.byId(2).last_name))
  })

  it('says so when there is nothing left to undo', async () => {
    renderTriage({ contacts: 3 })
    await currentName()

    press('u')

    expect(await screen.findByRole('alert')).toHaveTextContent(/nothing left to undo/i)
  })

  it('does not put back a contact this queue would not serve', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    fireEvent.click(screen.getByRole('button', { name: 'Skipped' }))
    await screen.findByText(/nothing skipped is left/i)

    press('u')

    // The contact really was restored...
    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    // ...but it is untriaged, and this queue is the skipped, so it is reported
    // rather than rendered as the next card.
    expect(await screen.findByText(/not in this queue/i)).toBeInTheDocument()
    expect(screen.queryByTestId('triage-card')).not.toBeInTheDocument()
  })

  it('names what u will take back, and warns that a tag is not on the stack', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    expect(screen.getByTestId('undo-affordance')).toHaveTextContent(
      'u takes back the newest triage decision.',
    )

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(screen.getByTestId('undo-affordance')).toHaveTextContent(/u takes back: met — Ada/)

    press('t')
    const picker = await screen.findByRole('group', { name: 'Tag this contact' })
    fireEvent.click(await screen.findByRole('button', { name: /founder/ }))
    await waitFor(() => expect(backend.byId(2).tags).toHaveLength(1))

    // `u` would reach past the tag to the decision before it, and says so.
    expect(screen.getByTestId('undo-affordance')).toHaveTextContent(
      /Tagging is not on the triage undo stack/i,
    )
    expect(screen.getByTestId('undo-affordance')).toHaveTextContent(/met — Ada/)
    expect(picker).toBeInTheDocument()
  })
})

describe('undo against the service’s own conflict rule', () => {
  it('is not refused because something else renamed the contact', async () => {
    // `_diverged` iterates `row.after_state`, and a decide row records only
    // `met` and `triaged_at` — so a rename in between is not this decision's
    // business and undo goes through. The fake used to record `preferred_name`
    // on a decide row and enforce it, which meant this screen was exercising a
    // refusal the API cannot produce.
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    backend.diverge(1, { preferred_name: 'Adelaide' })

    press('u')

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    // The rename stands: undo put back what the decision recorded, and the
    // decision never recorded a name.
    expect(backend.byId(1).preferred_name).toBe('Adelaide')
  })

  it('is refused when the field the decision did record has moved', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    backend.diverge(1, { triaged_at: '2030-01-01T00:00:00.000Z' })

    press('u')

    const prompt = await screen.findByRole('alertdialog')
    expect(prompt).toHaveTextContent(/changed since you decided/i)
    expect(backend.byId(1).met).toBe('met')
  })
})

/**
 * Issue #137: undo removes the row focus was standing on.
 *
 * The queue list draws every contact this run has passed as a button. Undoing
 * a decision puts that contact back at the front of the queue, so their row
 * leaves the trail — and if focus was on it, the browser drops focus to
 * `document.body` and a keyboard or screen-reader user loses their place
 * entirely (WCAG 2.4.3).
 *
 * Where focus goes is the judgement the issue asked for: it goes to the row
 * that took the same place in the list, which keeps the person where they were
 * reading rather than moving them to a control they were not using.
 */
describe('focus when undo takes a row out of the list', () => {
  it('hands it to the row that took its place, never to the document', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()
    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('m')
    await waitFor(() => expect(backend.byId(2).met).toBe('met'))

    const list = await screen.findByTestId('triage-queue-list')
    const row = within(list).getByRole('button', { name: /Bo Sample-2 — Met/ })
    row.focus()
    expect(document.activeElement).toBe(row)

    press('u')

    await waitFor(() => expect(backend.byId(2).met).toBe('unknown'))
    // The row is gone: Bo is back at the front of the queue, not behind you.
    await waitFor(() =>
      expect(within(list).queryByRole('button', { name: /Bo Sample-2 — Met/ })).toBeNull(),
    )
    expect(document.activeElement).not.toBe(document.body)
    expect(list.contains(document.activeElement)).toBe(true)
    // The same index, which is the row that took its place.
    expect(document.activeElement).toHaveAccessibleName(/Bo Sample-2 — On screen/)
  })

  it('leaves focus alone when it was never on the row that went', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()
    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))

    const filter = screen.getByRole('button', { name: 'Untriaged' })
    filter.focus()
    press('u')

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    expect(document.activeElement).toBe(filter)
  })
})
