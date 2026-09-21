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
 * The backend here is a fake with a fixed 15 ms delay, which is generous for a
 * local FastAPI process over SQLite on loopback. What the test cannot measure is
 * a person reading the evidence panel, so the honest form of the result is
 * "the screen adds N ms of the twelve-second budget", and N is printed below.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderTriage } from './test-render'

const CONTACTS = 50
/** Stands in for a local backend answering over loopback. */
const LATENCY_MS = 15
/** Spec: fifty in ten minutes. */
const BUDGET_MS = 10 * 60 * 1000

function cardName(): string {
  return screen.queryByTestId('triage-card')?.querySelector('h2')?.textContent ?? ''
}

describe('fifty contacts on the keyboard alone', () => {
  it('costs no wait at a pace a person could work at', async () => {
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
  })

  it('takes fifty decisions in one request each, far inside the ten minutes', async () => {
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
    // suggestion preview, and nothing else: the decision *is* the fetch.
    expect(backend.countOf('/api/v1/triage/next', 'GET')).toBe(1)
    expect(backend.countOf('/api/v1/triage/suggestions', 'GET')).toBe(1)
    expect(backend.seen).toHaveLength(CONTACTS + 2)

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
    // Two orders of magnitude inside the twelve seconds a contact is allowed,
    // so the screen is never what a run is waiting for.
    expect(elapsed / CONTACTS).toBeLessThan(120)
    expect(worst).toBeLessThan(1_000)
  })
})
