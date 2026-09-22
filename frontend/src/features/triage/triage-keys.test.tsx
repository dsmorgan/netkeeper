/**
 * Every key in spec 10.2, and the one thing `→` must not do.
 *
 * The assertions go through the fake backend's state rather than through the
 * request bodies, because what matters is that pressing `m` leaves the contact
 * marked met — not that some request went out.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { currentName, renderTriage } from './test-render'

function press(key: string) {
  return fireEvent.keyDown(window, { key })
}

describe('the triage keyboard map (spec 10.2)', () => {
  it('m marks the contact met and moves to the next one', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    expect(await currentName()).toContain('Ada')

    press('m')

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(await currentName()).toContain('Bo')
  })

  it('n marks the contact not met', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('n')

    await waitFor(() => expect(backend.byId(1).met).toBe('not_met'))
  })

  it('s skips, and the skipped contact is revisitable under the filter', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('s')
    await waitFor(() => expect(backend.byId(1).met).toBe('skip'))
    expect(await currentName()).toContain('Bo')

    fireEvent.click(screen.getByRole('button', { name: 'Skipped' }))
    await waitFor(async () => expect(await currentName()).toContain('Ada'))
  })

  it('→ moves on without deciding and writes nothing', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    expect(await currentName()).toContain('Ada')

    press('ArrowRight')

    expect(await currentName()).toContain('Bo')
    await waitFor(() => expect(backend.countOf('/api/v1/triage/next')).toBe(2))
    expect(backend.countOf('/api/v1/triage/decisions')).toBe(0)
    expect(backend.decisions).toHaveLength(0)
    expect(backend.byId(1).met).toBe('unknown')
    expect(backend.byId(1).triaged_at).toBeNull()
  })

  it('→ asks for the contact past the frontier, never one already passed', async () => {
    const { backend } = renderTriage({ contacts: 6 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('ArrowRight')
    expect(await currentName()).toContain('Cleo')

    press('m')
    await waitFor(() => expect(backend.byId(3).met).toBe('met'))
    // Bo was passed over, not decided, and never came back around.
    expect(backend.byId(2).met).toBe('unknown')
    expect(await currentName()).toContain('Dev')
  })

  it('t opens the tag picker and applies a tag', async () => {
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    press('t')

    const picker = await screen.findByRole('group', { name: 'Tag this contact' })
    fireEvent.click(await within(picker).findByRole('button', { name: /founder/ }))
    await waitFor(() => expect(backend.byId(1).tags).toHaveLength(1))
    expect(backend.byId(1).tags[0]?.name).toBe('founder')
  })

  it('t makes a tag that does not exist yet and puts it on, without leaving the run', async () => {
    // The thing somebody thinks of while looking at a contact: "I met this one
    // through that tool" is not a rule and never will be, and going away to
    // make the tag loses the run and the thought.
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    press('t')
    const picker = await screen.findByRole('group', { name: 'Tag this contact' })
    fireEvent.change(within(picker).getByLabelText('Tag'), { target: { value: 'met at a meetup' } })

    fireEvent.click(await within(picker).findByRole('button', { name: /^Make/ }))

    await waitFor(() => expect(backend.byId(1).tags).toHaveLength(1))
    expect(backend.byId(1).tags[0]?.name).toBe('met at a meetup')
    // A tag the person put on themselves, which is what the batches read.
    expect(backend.byId(1).tags[0]?.kind).toBe('manual')
    // Asserted on the wire, not on what the fake made of it: the server
    // defaults this field the same way, so a client that stopped sending it
    // would look right here and be wrong about what it asked for.
    const made = backend.seen.find(
      (request) => request.method === 'POST' && request.path === '/api/v1/tags',
    )
    expect(made?.body).toEqual({ name: 'met at a meetup', kind: 'manual' })
    // And it is on file for the next contact, without a reload.
    expect(within(picker).getByRole('button', { name: /met at a meetup/ })).toBeInTheDocument()
    expect(await within(picker).findByRole('button', { name: /founder/ })).toBeInTheDocument()
  })

  it('t makes the tag on Enter, so a run never needs the mouse', async () => {
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    press('t')
    const picker = await screen.findByRole('group', { name: 'Tag this contact' })
    const field = within(picker).getByLabelText('Tag')
    fireEvent.change(field, { target: { value: 'ran a workshop with' } })
    fireEvent.keyDown(field, { key: 'Enter' })

    await waitFor(() => expect(backend.byId(1).tags).toHaveLength(1))
    expect(backend.byId(1).tags[0]?.name).toBe('ran a workshop with')
    // The box empties, so the next thing typed is the next tag.
    expect(field).toHaveValue('')
  })

  it('offers no Make for a name already on file, whatever its case', async () => {
    renderTriage({ contacts: 2 })
    await currentName()

    press('t')
    const picker = await screen.findByRole('group', { name: 'Tag this contact' })
    fireEvent.change(within(picker).getByLabelText('Tag'), { target: { value: '  FOUNDER ' } })

    expect(await within(picker).findByRole('button', { name: /founder/ })).toBeInTheDocument()
    expect(within(picker).queryByRole('button', { name: /^Make/ })).not.toBeInTheDocument()
  })

  it('puts on the tag that was already there rather than reporting a conflict', async () => {
    // Another tab made it after this picker read the list. The person meant
    // "this contact has that tag", and that is what they get.
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    press('t')
    const picker = await screen.findByRole('group', { name: 'Tag this contact' })
    fireEvent.change(within(picker).getByLabelText('Tag'), {
      target: { value: 'spoke at a panel' },
    })
    backend.addTagBehindTheScenes('Spoke at a panel')

    fireEvent.click(await within(picker).findByRole('button', { name: /^Make/ }))

    await waitFor(() => expect(backend.byId(1).tags).toHaveLength(1))
    expect(backend.byId(1).tags[0]?.name).toBe('Spoke at a panel')
    expect(within(picker).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('t says that tagging is not on the triage undo stack', async () => {
    renderTriage({ contacts: 2 })
    await currentName()

    press('t')

    const picker = await screen.findByRole('group', { name: 'Tag this contact' })
    expect(picker).toHaveTextContent(/not on the triage undo stack/i)
  })

  it('closes the p editor when the card moves on, so a name cannot land on the wrong contact', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('p')
    const field = await screen.findByLabelText('Preferred name')
    fireEvent.change(field, { target: { value: 'Addie' } })
    // Focus leaves the field, so the global map is live again.
    screen.getByRole('button', { name: 'Untriaged' }).focus()
    press('m')

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(await currentName()).toContain('Bo')
    // The editor belonged to Ada; it is not sitting over Bo holding her name.
    expect(screen.queryByLabelText('Preferred name')).not.toBeInTheDocument()
    expect(backend.byId(2).preferred_name).toBe('Bo')
    expect(backend.byId(1).preferred_name).toBe('Ada')
  })

  it('closes the tag picker when the card moves on', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('t')
    await screen.findByRole('group', { name: 'Tag this contact' })
    screen.getByRole('button', { name: 'Untriaged' }).focus()
    press('m')

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(screen.queryByRole('group', { name: 'Tag this contact' })).not.toBeInTheDocument()
  })

  it('p edits the preferred name, and typing in it does not decide', async () => {
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    press('p')

    const field = await screen.findByLabelText('Preferred name')
    fireEvent.change(field, { target: { value: 'Addie' } })
    // "m" typed into the field is a letter, not a decision.
    fireEvent.keyDown(field, { key: 'm' })
    expect(backend.byId(1).met).toBe('unknown')

    fireEvent.submit(field.closest('form')!)
    await waitFor(() => expect(backend.byId(1).preferred_name).toBe('Addie'))
    expect(await currentName()).toContain('Addie')
  })

  it('? opens the help overlay, lists every key, and does not steal the keyboard', async () => {
    const { backend } = renderTriage({ contacts: 3 })
    await currentName()

    press('?')

    const help = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(help).toHaveAttribute('aria-modal', 'false')
    for (const label of ['m', 'n', 's', 'u', 't', 'p', '←', '→', '?', 'Esc']) {
      expect(within(help).getByText(label, { selector: 'kbd' })).toBeInTheDocument()
    }

    // The map still works while the overlay is open.
    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeInTheDocument()

    press('Escape')
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).not.toBeInTheDocument(),
    )
  })

  it('stands aside for browser shortcuts and for keys it does not own', async () => {
    const { backend } = renderTriage({ contacts: 3 })
    await currentName()

    fireEvent.keyDown(window, { key: 'm', metaKey: true })
    fireEvent.keyDown(window, { key: 'q' })
    fireEvent.keyDown(window, { key: 'ArrowDown' })

    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(backend.byId(1).met).toBe('unknown')
    expect(backend.countOf('/api/v1/triage/decisions')).toBe(0)
  })

  it('ignores an auto-repeat, so a resting finger decides one contact and not thirty', async () => {
    const { backend } = renderTriage({ contacts: 20 })
    await currentName()

    // One real press, then the key held down.
    press('m')
    for (let index = 0; index < 19; index += 1) {
      fireEvent.keyDown(window, { key: 'm', repeat: true })
    }

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(backend.contacts.filter((contact) => contact.met !== 'unknown')).toHaveLength(1)
    expect(backend.countOf('/api/v1/triage/decisions', 'POST')).toBe(1)
  })

  it('ignores an auto-repeat on → too, which would skim past unread contacts', async () => {
    const { backend } = renderTriage({ contacts: 20 })
    await currentName()

    for (let index = 0; index < 10; index += 1) {
      fireEvent.keyDown(window, { key: 'ArrowRight', repeat: true })
    }

    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(await currentName()).toContain('Ada')
    expect(backend.countOf('/api/v1/triage/next')).toBe(1)
  })

  it('asks for the contact past the frontier when → outruns the refill', async () => {
    // The cursor is the furthest contact the server has handed over, remembered
    // rather than read off the buffer. Pressed twice before the first refill
    // lands, a buffer-derived cursor asks past a card that is no longer there
    // and hands back somebody already passed over.
    const { backend } = renderTriage({ contacts: 8, latencyMs: 40 })
    expect(await currentName()).toContain('Ada')

    press('ArrowRight') // past Ada; the buffer still holds Bo
    press('ArrowRight') // past Bo, before the refill for Cleo has landed

    await waitFor(async () => expect(await currentName()).toContain('Cleo'))
    await waitFor(() => expect(backend.countOf('/api/v1/triage/next')).toBe(3))

    // The mount, then past contact 2, then past contact 3. Never past 2 twice.
    const cursors = backend.seen
      .filter((entry) => entry.path === '/api/v1/triage/next')
      .map((entry) => entry.search.get('after_id'))
    expect(cursors).toEqual([null, '2', '3'])
    // Nobody was decided, and nobody was shown twice.
    expect(backend.decisions).toHaveLength(0)
  })

  it('says a key was ignored rather than swallowing it (issue #92)', async () => {
    // Six `m` presses against a slow backend used to decide the two cards in
    // hand and drop the other four in silence, which is not something a
    // keyboard-first screen may do.
    const { backend } = renderTriage({ contacts: 6, latencyMs: 60 })
    await currentName()

    for (let index = 0; index < 6; index += 1) press('m')

    await waitFor(() =>
      expect(screen.getByTestId('triage-notice')).toHaveTextContent(/so m did nothing/i),
    )
    expect(screen.getByTestId('triage-notice')).toHaveTextContent(/Nothing was recorded/i)
    // Exactly the two that had a card, and no request for the four that did not.
    await waitFor(() =>
      expect(backend.contacts.filter((contact) => contact.met !== 'unknown')).toHaveLength(2),
    )
    expect(backend.countOf('/api/v1/triage/decisions', 'POST')).toBe(2)
  })

  it('prevents the default for a key it handles, so nothing scrolls', async () => {
    renderTriage({ contacts: 3 })
    await currentName()

    expect(press('ArrowRight')).toBe(false)
    expect(press('ArrowLeft')).toBe(false)
    expect(fireEvent.keyDown(window, { key: 'z' })).toBe(true)
  })
})
