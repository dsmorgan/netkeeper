/**
 * The P1-14 budget, measured: fifty contacts in under ten minutes, keyboard only.
 *
 * Ten minutes for fifty is twelve seconds each, which is a budget on the
 * *interaction*, not on the render. Two things are measured here, because one
 * without the other would be a half-truth:
 *
 * 1. **At any human pace, a decision costs no wait at all.** The card changes in
 *    the same tick as the keystroke — asserted with no `await` between the
 *    `keyDown` and reading the card. That is the claim the budget rests on: the
 *    next contact is already in hand when you press `m`.
 * 2. **Even driven flat out**, fifty keystrokes go through in a couple of
 *    seconds against a backend that takes 15 ms to answer, and cost exactly one
 *    request each.
 *
 * The first of those is the load-bearing one. The second is a regression guard
 * on the request count, and its wall time says as much about the fake as about
 * the screen: the backend here has a fixed 15 ms delay, which dominates the
 * per-contact figure. Driven against a real FastAPI/SQLite backend the same
 * fifty decisions took 210 ms, 4.2 ms each, with an identical request count and
 * ordering — so the delay here is conservative, not flattering, and the number
 * printed below is an upper bound rather than the result.
 *
 * 3. **The same fifty on the mouse alone** (P1-23). The buttons go through the
 *    same handler as the keys, so this is a guard on that staying true: same
 *    request count, same ordering, same order of magnitude in wall time. It is
 *    the issue's "fifty contacts can be triaged with the mouse alone as well as
 *    the keyboard alone", measured rather than asserted.
 *
 * What none of them measures is a person reading the evidence panel. The honest
 * form of the result is "the screen adds a few milliseconds of the twelve
 * seconds a contact gets", not "a contact takes N ms".
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderTriage } from './test-render'

const CONTACTS = 50
/** Stands in for a local backend answering over loopback. */
const LATENCY_MS = 15
/** Spec: fifty in ten minutes. */
const BUDGET_MS = 10 * 60 * 1000
/**
 * The runner's own patience, which is not the budget and does not carry weight.
 *
 * On a 4-core machine (#220) the keyboard run takes about 1 s alone and 2.3 s
 * with the backend suite competing; the mouse run takes 3 s and 5.5 s, which
 * is past vitest's default five seconds. This is headroom, not a workaround: a
 * regression large enough to matter trips the per-contact ceilings below first,
 * at 6 s (keyboard) and 12.5 s (mouse) for fifty.
 */
const TEST_TIMEOUT_MS = 20_000

/**
 * How long a contact may take here, which is *not* the twelve seconds.
 *
 * The spec's budget is 600 000 ms for fifty; this is the tighter regression
 * guard, two orders of magnitude inside it. What is left after the fake's own
 * 15 ms is mostly testing-library — `waitFor` polling and the re-render it
 * queries — so it is a ceiling on the harness plus the screen together, and
 * only a regression large enough to clear that ceiling shows up here.
 */
const PER_CONTACT_CEILING_MS = 120

/**
 * The same guard for the mouse, which the harness makes slower than the
 * keyboard: each decision is two `waitFor` polls and a click, not a keydown.
 * Measured on a 4-core machine (#220): 60 ms a contact alone, 110 ms with the
 * backend suite competing for it, which the keyboard's 120 ms left no room
 * for. The keyboard took 20 and 46 ms on the same runs, so its ceiling stays.
 */
const MOUSE_PER_CONTACT_CEILING_MS = 250

function cardName(): string {
  return screen.queryByTestId('triage-card')?.querySelector('h2')?.textContent ?? ''
}

describe('fifty contacts on the keyboard alone', () => {
  it(
    'costs no wait at a pace a person could work at',
    async () => {
      const { backend } = renderTriage({ contacts: 12, latencyMs: LATENCY_MS })
      await screen.findByTestId('triage-card')

      let synchronous = 0
      for (let index = 0; index < 10; index += 1) {
        // Far less than the twelve seconds a contact gets, and well past the
        // round trip, so the prefetch has landed.
        await new Promise((resolve) => setTimeout(resolve, LATENCY_MS * 4))
        const showing = cardName()
        fireEvent.keyDown(window, { key: 'm' })
        // No await: the next contact is already rendered.
        if (cardName() !== showing) synchronous += 1
      }

      expect(synchronous).toBe(10)
      await waitFor(() => expect(backend.countOf('/api/v1/triage/decisions', 'POST')).toBe(10))
    },
    TEST_TIMEOUT_MS,
  )

  it(
    'takes fifty decisions in one request each, far inside the ten minutes',
    async () => {
      const { backend } = renderTriage({ contacts: CONTACTS, latencyMs: LATENCY_MS })
      await screen.findByTestId('triage-card')

      const perContact: number[] = []
      const started = performance.now()
      for (let index = 0; index < CONTACTS; index += 1) {
        const at = performance.now()
        // Driven flat out the two cards in hand run out, so part of the cost of a
        // contact is the refill. It is counted here rather than started after it.
        await waitFor(() => expect(cardName()).not.toBe(''), { interval: 1, timeout: 5_000 })
        const showing = cardName()
        fireEvent.keyDown(window, { key: index % 3 === 1 ? 'n' : 'm' })
        await waitFor(() => expect(cardName()).not.toBe(showing), { interval: 1, timeout: 5_000 })
        perContact.push(performance.now() - at)
      }
      const elapsed = performance.now() - started

      // Every contact got an answer, and each one cost exactly one request. The
      // last answer is still in flight when the loop ends, so this waits for the
      // writes rather than for the requests.
      await waitFor(() =>
        expect(backend.contacts.filter((contact) => contact.met !== 'unknown')).toHaveLength(
          CONTACTS,
        ),
      )
      expect(backend.countOf('/api/v1/triage/decisions', 'POST')).toBe(CONTACTS)
      expect(new Set(backend.decisions.map((decision) => decision.contact_id)).size).toBe(CONTACTS)
      // One `GET /triage/next` to prime the two cards in hand, one for the
      // suggestion preview, one page of the contacts ahead, and nothing else:
      // the decision *is* the fetch. Fifty fit inside the hundred-row
      // look-ahead page, so the run never asks for a second one, which is the
      // property this exact count exists to hold.
      expect(backend.countOf('/api/v1/triage/next', 'GET')).toBe(1)
      expect(backend.countOf('/api/v1/triage/suggestions', 'GET')).toBe(1)
      expect(backend.countOf('/api/v1/contacts/query', 'POST')).toBe(1)
      expect(backend.seen).toHaveLength(CONTACTS + 3)

      const sorted = [...perContact].sort((a, b) => a - b)
      const median = sorted[Math.floor(sorted.length / 2)] ?? 0
      const worst = sorted.at(-1) ?? 0
      console.info(
        `[P1-14] ${CONTACTS} keyboard decisions in ${elapsed.toFixed(0)} ms ` +
          `(${(elapsed / CONTACTS).toFixed(1)} ms each, median ${median.toFixed(1)} ms, ` +
          `worst ${worst.toFixed(1)} ms) against a ${LATENCY_MS} ms backend, ` +
          `${backend.seen.length} requests. Budget is ${BUDGET_MS} ms.`,
      )

      expect(elapsed).toBeLessThan(BUDGET_MS)
      expect(elapsed / CONTACTS).toBeLessThan(PER_CONTACT_CEILING_MS)
      expect(worst).toBeLessThan(1_000)
    },
    TEST_TIMEOUT_MS,
  )

  it(
    'takes the same fifty on the mouse alone, for the same cost',
    async () => {
      const { backend } = renderTriage({ contacts: CONTACTS, latencyMs: LATENCY_MS })
      await screen.findByTestId('triage-card')

      // Looked up once, not once a contact. `getByRole` scans the whole
      // document for accessible names, which costs several times what this test
      // is trying to measure; leaving it inside the loop made most of the
      // published mouse figure testing-library rather than the screen. The
      // polling interval matches the keyboard loop's, so the two rows below are
      // comparable.
      //
      // The decision buttons are on the card now (#142), and driven flat out
      // the two cards in hand do run out — which unmounts the card, and with it
      // the node this cached. So the cached node is re-taken when, and only
      // when, it has left the document: an `isConnected` check costs nothing
      // and keeps the accessible-name scan out of the measured path, where it
      // used to dominate the figure.
      const buttons = new Map<string, HTMLElement>()
      const button = (name: string) => {
        const held = buttons.get(name)
        if (held !== undefined && held.isConnected) return held
        const found = screen.getByRole('button', { name })
        buttons.set(name, found)
        return found
      }

      const started = performance.now()
      for (let index = 0; index < CONTACTS; index += 1) {
        await waitFor(() => expect(cardName()).not.toBe(''), { interval: 1, timeout: 5_000 })
        const showing = cardName()
        fireEvent.click(button(index % 3 === 1 ? 'Not met' : 'Met'))
        await waitFor(() => expect(cardName()).not.toBe(showing), { interval: 1, timeout: 5_000 })
      }
      const elapsed = performance.now() - started

      await waitFor(() =>
        expect(backend.contacts.filter((contact) => contact.met !== 'unknown')).toHaveLength(
          CONTACTS,
        ),
      )
      // The button row is not a second path to the API: it is the same one,
      // with the same three reads around it.
      expect(backend.countOf('/api/v1/triage/decisions', 'POST')).toBe(CONTACTS)
      expect(new Set(backend.decisions.map((decision) => decision.contact_id)).size).toBe(CONTACTS)
      expect(backend.seen).toHaveLength(CONTACTS + 3)

      console.info(
        `[P1-23] ${CONTACTS} mouse decisions in ${elapsed.toFixed(0)} ms ` +
          `(${(elapsed / CONTACTS).toFixed(1)} ms each) against a ${LATENCY_MS} ms backend, ` +
          `${backend.seen.length} requests. Budget is ${BUDGET_MS} ms.`,
      )
      expect(elapsed).toBeLessThan(BUDGET_MS)
      expect(elapsed / CONTACTS).toBeLessThan(MOUSE_PER_CONTACT_CEILING_MS)
    },
    TEST_TIMEOUT_MS,
  )
})
