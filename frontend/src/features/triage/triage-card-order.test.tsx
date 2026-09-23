/**
 * The order the card teaches, and the explainer above it (#142).
 *
 * CP2.5's first real run produced two complaints about this screen that are
 * really one: the met call led everything, and nothing on the screen said what
 * it meant. Tagging somebody and fixing what you call them are what you notice
 * while you are reading their headline — they come *before* the decision, and
 * before this the screen taught the opposite, with the decision buttons leading
 * the row and both editors hidden behind keys until you pressed one.
 *
 * What must not change is the cost of a run. Every assertion about the new
 * order is about what is *drawn*; `triage-keys.test.tsx` and
 * `triage-throughput.test.tsx` hold the other half, that `m`, `n`, `s`, `t` and
 * `p` still fire from anywhere for the same one request each.
 *
 * The three answers are drawn once, on the card. `triage-controls.test.tsx`
 * holds that they are not also in the action row above it; this file holds
 * what the two rows do contain.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { BAR_BINDINGS, DECISION_BINDINGS } from './keymap'
import { DECISION_MEANINGS } from './method'
import { createFakeBackend } from './test-backend'
import { currentName, renderTriage } from './test-render'

function press(key: string) {
  fireEvent.keyDown(window, { key })
}

/** A backend whose first contact already carries a tag, without opening the picker. */
function withATagOnAda() {
  const backend = createFakeBackend({ contacts: 4 })
  backend.byId(1).tags.push({ id: 1, name: 'founder', color: null, kind: 'manual' })
  return backend
}

describe('the card, in the order the work is done', () => {
  it('reads name, then tags, then the decision', async () => {
    renderTriage({ contacts: 4 })
    await currentName()

    const steps = screen.getByTestId('triage-steps')
    // One assertion on the rendered order rather than three on the parts: the
    // whole point of the change is which comes first.
    expect(steps.textContent).toMatch(/Name[\s\S]*Tags[\s\S]*Have you met Ada\?/)

    const card = screen.getByTestId('triage-card')
    expect(card.compareDocumentPosition(steps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows the tags a contact already carries without anything being opened', async () => {
    renderTriage({ backend: withATagOnAda() })
    await currentName()

    const steps = screen.getByTestId('triage-steps')
    expect(steps).toHaveTextContent('founder')
    // The picker is what `t` opens; the tags themselves are not behind it.
    expect(screen.queryByRole('group', { name: 'Tag this contact' })).not.toBeInTheDocument()
  })

  it('shows the preferred name without anything being opened', async () => {
    renderTriage({ contacts: 4 })
    await currentName()

    const name = within(screen.getByTestId('triage-steps'))
    expect(name.getByRole('button', { name: /^Edit/ })).toBeInTheDocument()
    expect(screen.queryByLabelText('Preferred name')).not.toBeInTheDocument()
    expect(screen.getByTestId('triage-steps')).toHaveTextContent('Ada')
  })

  it('opens both editors in place, inside the card rather than under it', async () => {
    renderTriage({ contacts: 4 })
    await currentName()
    const steps = screen.getByTestId('triage-steps')

    press('p')
    expect(steps.contains(await screen.findByLabelText('Preferred name'))).toBe(true)

    press('Escape')
    press('t')
    expect(steps.contains(await screen.findByRole('group', { name: 'Tag this contact' }))).toBe(
      true,
    )
  })

  it('takes a tag off from the card, without opening the picker', async () => {
    const backend = withATagOnAda()
    renderTriage({ backend })
    await currentName()

    fireEvent.click(screen.getByRole('button', { name: 'Take the tag founder off Ada Example-1' }))

    await waitFor(() => expect(backend.byId(1).tags).toHaveLength(0))
    expect(backend.seen.some((entry) => entry.method === 'DELETE')).toBe(true)
  })

  it('decides from the card itself, through the same handler the key uses', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    expect(await currentName()).toContain('Ada')

    fireEvent.click(screen.getByRole('button', { name: 'Met' }))

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(await currentName()).toContain('Bo')
    expect(backend.countOf('/api/v1/triage/decisions', 'POST')).toBe(1)
  })

  it.each([
    ['Not met', 'not_met'],
    ['Skip', 'skip'],
  ])("records %s from the card's own button", async (term, expected) => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    fireEvent.click(screen.getByRole('button', { name: term }))

    await waitFor(() => expect(backend.byId(1).met).toBe(expected))
  })

  it('asks the question about the person on screen, and follows the card', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()
    expect(screen.getByTestId('triage-steps')).toHaveTextContent('Have you met Ada?')

    press('m')

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(screen.getByTestId('triage-steps')).toHaveTextContent('Have you met Bo?')
  })

  it('defines each answer beside the button that gives it', async () => {
    renderTriage({ contacts: 4 })
    await currentName()

    const meanings = screen.getByTestId('decision-meanings')
    for (const meaning of DECISION_MEANINGS) {
      expect(meanings).toHaveTextContent(meaning.short)
    }
  })
})

describe('the button row', () => {
  it('leads with Name and Tag, and carries no decision at all', async () => {
    renderTriage({ contacts: 4 })
    await currentName()

    const bar = screen.getByTestId('triage-actions')
    const drawn = within(bar)
      .getAllByRole('button')
      .map((button) => button.textContent?.replace(/\s+$/, ''))
    expect(drawn).toEqual(['Namep', 'Tagt', 'Back←', 'Next→', 'Undou', 'Keyboard?'])
    // Still generated from the map, so nothing fell out of it silently.
    expect(drawn).toHaveLength(BAR_BINDINGS.length)
  })

  it('puts the decisions on the card, in the order they are asked', async () => {
    renderTriage({ contacts: 4 })
    await currentName()

    const drawn = within(screen.getByTestId('triage-decision'))
      .getAllByRole('button')
      .map((button) => button.textContent?.replace(/\s+$/, ''))
    expect(drawn).toEqual(['Metm', 'Not metn', 'Skips'])
    expect(drawn).toHaveLength(DECISION_BINDINGS.length)
  })
})

describe('the explainer above the card', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    try {
      window.localStorage.clear()
    } catch {
      // Nothing stored, nothing to clear.
    }
  })

  it('is open the first time, in the method’s own words', async () => {
    renderTriage({ contacts: 4 })
    await currentName()

    const explainer = screen.getByTestId('triage-explainer')
    expect(explainer).toHaveTextContent('Decide who in your network you have actually met')
    expect(explainer).toHaveTextContent('in person, on a video call, or in a real conversation')
    expect(explainer).toHaveTextContent('If you have met them once, they count')
    expect(explainer).toHaveTextContent('never actually spoken with')
    expect(within(explainer).getByRole('button')).toHaveAttribute('aria-expanded', 'true')
  })

  it('stays collapsed for the next visit once it is collapsed', async () => {
    const first = renderTriage({ contacts: 4 })
    await currentName()
    fireEvent.click(within(screen.getByTestId('triage-explainer')).getByRole('button'))
    expect(screen.getByTestId('triage-explainer')).not.toHaveTextContent(
      'If you have met them once, they count',
    )
    first.unmount()

    renderTriage({ contacts: 4 })
    await currentName()

    const explainer = screen.getByTestId('triage-explainer')
    expect(within(explainer).getByRole('button')).toHaveAttribute('aria-expanded', 'false')
    // The goal line is not part of what collapses: it is one sentence, and it
    // is the thing somebody glancing at the screen needs.
    expect(explainer).toHaveTextContent('Decide who in your network you have actually met')
  })

  it('opens again when it is expanded, and stays open', async () => {
    const first = renderTriage({ contacts: 4 })
    await currentName()
    const toggle = () =>
      fireEvent.click(within(screen.getByTestId('triage-explainer')).getByRole('button'))
    toggle()
    toggle()
    first.unmount()

    renderTriage({ contacts: 4 })
    await currentName()

    expect(within(screen.getByTestId('triage-explainer')).getByRole('button')).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('renders, open, when storage throws the way a private window does', async () => {
    // Not "comes back empty": `localStorage` can throw on the getter itself
    // with site data blocked, and an unguarded read would take the whole
    // screen down with it rather than one collapsed section.
    const denied = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('access to storage is not allowed from this context')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('access to storage is not allowed from this context')
    })

    renderTriage({ contacts: 4 })
    await currentName()

    expect(screen.getByTestId('triage-explainer')).toHaveTextContent(
      'If you have met them once, they count',
    )
    expect(denied).toHaveBeenCalled()
    // And collapsing it still works for as long as the page is open.
    fireEvent.click(within(screen.getByTestId('triage-explainer')).getByRole('button'))
    expect(screen.getByTestId('triage-explainer')).not.toHaveTextContent(
      'If you have met them once, they count',
    )
  })
})
