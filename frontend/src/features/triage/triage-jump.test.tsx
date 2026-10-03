/**
 * Jump to a contact in the queue without deciding the ones before them (#322).
 *
 * What matters: the named contact is on screen next, nobody before them was
 * written to, the normal order resumes afterwards, and the search offers only
 * who the queue being served would serve.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { createFakeBackend } from './test-backend'
import { currentName, renderTriage } from './test-render'

function searchBox(): HTMLElement {
  return screen.getByRole('searchbox', { name: 'Search the queue by name' })
}

async function jumpTo(name: string): Promise<void> {
  fireEvent.change(searchBox(), { target: { value: name } })
  const matches = await screen.findByRole('list', { name: 'Matches' })
  fireEvent.click(within(matches).getByRole('button', { name: new RegExp(name) }))
}

describe('jump to a contact', () => {
  it('serves the named contact next, then carries on in the normal order', async () => {
    const backend = createFakeBackend({ contacts: 8 })
    renderTriage({ backend })
    const first = await currentName()
    const target = backend.byId(6)
    const targetName = `${target.preferred_name} ${target.last_name}`

    await jumpTo(target.last_name)
    await waitFor(async () => expect(await currentName()).toBe(targetName))
    // Nobody before them was decided.
    expect(backend.decisions).toHaveLength(0)
    expect(backend.byId(1).met).toBe('unknown')

    fireEvent.keyDown(window, { key: 'm' })
    await waitFor(() => expect(backend.decisions).toHaveLength(1))
    expect(backend.byId(6).met).toBe('met')
    // Back to the order the queue was in.
    await waitFor(async () => expect(await currentName()).toBe(first))
  })

  it('links the card name to the contact page, in a new tab', async () => {
    renderTriage({ contacts: 3 })
    await currentName()
    const link = within(screen.getByTestId('triage-card')).getByRole('link', { name: /Ada/ })
    expect(link).toHaveAttribute('href', '/contacts/1')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('moves a contact already in hand up rather than showing them twice', async () => {
    const backend = createFakeBackend({ contacts: 4 })
    renderTriage({ backend })
    await currentName()
    const second = backend.byId(2)

    await jumpTo(second.last_name)
    await waitFor(async () =>
      expect(await currentName()).toBe(`${second.preferred_name} ${second.last_name}`),
    )
    const list = screen.getByTestId('triage-queue-list')
    expect(within(list).getAllByText(second.last_name, { exact: false })).toHaveLength(1)
  })

  it('offers only contacts the queue being served holds', async () => {
    const backend = createFakeBackend({ contacts: 4 })
    backend.byId(3).met = 'met'
    renderTriage({ backend })
    await currentName()

    fireEvent.change(searchBox(), { target: { value: backend.byId(3).last_name } })
    expect(await screen.findByText(/Nobody in the untriaged matches/)).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Matches' })).not.toBeInTheDocument()
  })

  it('does not treat typing a name as a decision', async () => {
    const backend = createFakeBackend({ contacts: 3 })
    renderTriage({ backend })
    await currentName()

    const box = searchBox()
    box.focus()
    fireEvent.keyDown(box, { key: 'm' })
    fireEvent.keyDown(box, { key: 'n' })
    fireEvent.keyDown(box, { key: 's' })
    expect(backend.decisions).toHaveLength(0)
    expect(backend.byId(1).met).toBe('unknown')
  })

  it('says so when the contact left the queue before the jump landed', async () => {
    const backend = createFakeBackend({ contacts: 4 })
    renderTriage({ backend })
    await currentName()
    const target = backend.byId(4)

    fireEvent.change(searchBox(), { target: { value: target.last_name } })
    const matches = await screen.findByRole('list', { name: 'Matches' })
    // Decided in another tab between the search and the click.
    target.met = 'met'
    fireEvent.click(within(matches).getByRole('button', { name: new RegExp(target.last_name) }))

    expect(await screen.findByText(/Could not jump to that contact/)).toBeInTheDocument()
    expect(await currentName()).toBe(
      `${backend.byId(1).preferred_name} ${backend.byId(1).last_name}`,
    )
  })

  it('shows a contact once in the Both queue after a jump past the frontier and a skip', async () => {
    const backend = createFakeBackend({ contacts: 9 })
    renderTriage({ backend })
    await currentName()
    fireEvent.click(screen.getByRole('button', { name: 'Both' }))
    await waitFor(() => expect(backend.countOf('/api/v1/triage/next')).toBeGreaterThan(1))
    const target = backend.byId(6)

    await jumpTo(target.last_name)
    await waitFor(async () =>
      expect(await currentName()).toBe(`${target.preferred_name} ${target.last_name}`),
    )
    fireEvent.keyDown(window, { key: 's' })
    await waitFor(() => expect(backend.byId(6).met).toBe('skip'))

    // Five more answers walk the cursor past where the skipped contact sits.
    const seen: string[] = []
    for (let step = 0; step < 5; step += 1) {
      seen.push(await currentName())
      const before = backend.decisions.length
      fireEvent.keyDown(window, { key: 'm' })
      await waitFor(() => expect(backend.decisions.length).toBe(before + 1))
    }
    const next = backend.byId(7)
    await waitFor(async () =>
      expect(await currentName()).toBe(`${next.preferred_name} ${next.last_name}`),
    )
    expect(seen.filter((name) => name.endsWith(`-${target.id}`))).toHaveLength(0)
  })
})
