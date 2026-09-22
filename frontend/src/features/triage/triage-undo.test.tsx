/**
 * Undo: exact, refusable, and honest about what it will take back.
 *
 * The refusal is the interesting half. The API answers `409` and writes nothing
 * when the contact no longer holds what the decision left it holding — an edit
 * from elsewhere, or a contact archived or merged away since. The screen has to
 * put that to the person as a choice, not swallow it and not force it.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
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
