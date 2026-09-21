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
    expect(await screen.findByText(/no longer in this queue/i)).toBeInTheDocument()
    expect(await currentName()).toContain('Bo')
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
