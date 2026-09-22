/**
 * The states the screen has to hold: loading, error, empty, and the counters and
 * evidence in between — plus the accessibility a keyboard-first screen owes.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import { createFakeBackend } from './test-backend'
import { currentName, renderTriage } from './test-render'

describe('the triage screen', () => {
  it('says it is loading before the first answer', async () => {
    renderTriage({ contacts: 3, latencyMs: 30 })

    expect(screen.getByRole('status')).toHaveTextContent('Loading the queue…')
    expect(await currentName()).toContain('Ada')
  })

  it('offers a retry when the queue cannot be read', async () => {
    let failing = true
    renderTriage({
      contacts: 3,
      intercept: (request, next) => {
        if (failing && new URL(request.url).pathname === '/api/v1/triage/next') {
          return Promise.resolve(jsonResponse({ detail: 'the database is locked' }, 500))
        }
        return next(request)
      },
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/could not be read/i)

    failing = false
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(await currentName()).toContain('Ada')
  })

  it('says the queue is empty once everyone has an answer', async () => {
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    fireEvent.keyDown(window, { key: 'm' })
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    fireEvent.keyDown(window, { key: 'm' })
    await waitFor(() => expect(backend.byId(2).met).toBe('met'))

    expect(await screen.findByText('Nothing left to triage.')).toBeInTheDocument()
    expect(screen.queryByTestId('triage-card')).not.toBeInTheDocument()
  })

  it('points at the skipped from the empty state, and switches to them', async () => {
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    fireEvent.keyDown(window, { key: 's' })
    await waitFor(() => expect(backend.byId(1).met).toBe('skip'))
    fireEvent.keyDown(window, { key: 'm' })
    await waitFor(() => expect(backend.byId(2).met).toBe('met'))

    fireEvent.click(await screen.findByRole('button', { name: 'Revisit the skipped' }))
    await waitFor(async () => expect(await currentName()).toContain('Ada'))
  })

  it('counts triaged against total, and keeps up during a run', async () => {
    const { backend } = renderTriage({ contacts: 5 })
    await currentName()
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('0 / 5')

    fireEvent.keyDown(window, { key: 'm' })
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('1 / 5')
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('4 left in this queue')

    fireEvent.keyDown(window, { key: 's' })
    await waitFor(() => expect(backend.byId(2).met).toBe('skip'))
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('2 / 5')
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('1 skipped')
  })

  it('makes the card a live region so the next contact is announced', async () => {
    renderTriage({ contacts: 3 })

    const card = await screen.findByTestId('triage-card')
    expect(card).toHaveAttribute('aria-live', 'polite')
    expect(card).toHaveAttribute('aria-atomic', 'true')
    expect(card).toHaveAccessibleName('Contact under triage')

    // The evidence is a separate landmark: long, and not for announcing.
    const evidence = screen.getByRole('region', { name: 'Evidence' })
    expect(evidence).not.toHaveAttribute('aria-live')
  })

  it('does not move focus when the card changes', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()
    const filter = screen.getByRole('button', { name: 'Untriaged' })
    filter.focus()

    fireEvent.keyDown(window, { key: 'm' })

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(document.activeElement).toBe(filter)
  })

  it('renders a message body as text, whatever it contains (issue #75)', async () => {
    const backend = createFakeBackend({ contacts: 2, withMessages: 2 })
    backend.byId(1).messageBody =
      '<img src=x onerror="alert(1)">Loved the talk<script>alert(2)</script> — lunch soon?'
    renderTriage({ backend })

    const evidence = await screen.findByRole('region', { name: 'Evidence' })
    await waitFor(() => expect(evidence).toHaveTextContent(/Loved the talk/))
    expect(evidence.querySelector('img')).toBeNull()
    expect(evidence.querySelector('script')).toBeNull()
    expect(evidence.innerHTML).not.toContain('onerror')
  })

  it('does not print an image-only message body as markup (issue #92)', async () => {
    const backend = createFakeBackend({ contacts: 2, withMessages: 2 })
    backend.byId(1).messageBody = '<img src=x onerror="alert(1)">'
    renderTriage({ backend })

    const evidence = await screen.findByRole('region', { name: 'Evidence' })
    await waitFor(() => expect(evidence).toHaveTextContent(/No readable text in this message/))
    expect(evidence).not.toHaveTextContent('onerror')
    expect(evidence.innerHTML).not.toContain('onerror')
  })

  it('renders a note as text, markup and all (issue #92)', async () => {
    const backend = createFakeBackend({ contacts: 2 })
    backend.byId(1).notes = 'Intro to <img src=x onerror="alert(1)"> the ops team.\nCall Friday.'
    renderTriage({ backend })

    const evidence = await screen.findByRole('region', { name: 'Evidence' })
    await waitFor(() => expect(evidence).toHaveTextContent(/Intro to the ops team/))
    expect(evidence).not.toHaveTextContent('onerror')
    expect(evidence.innerHTML).not.toContain('onerror')
    // The line break a person typed survives the strip.
    expect(evidence.textContent).toContain('the ops team.\nCall Friday.')
  })

  it('leaves out a company nobody else in the address book is at', async () => {
    renderTriage({ contacts: 2, withMessages: 1 })

    const evidence = await screen.findByRole('region', { name: 'Evidence' })
    expect(evidence).toHaveTextContent('Example Corp')
    // The fake emits this one with `contact_count: 0`; it is not evidence.
    expect(evidence).not.toHaveTextContent('Nobody Else Here Ltd')
  })

  it('does not claim a shared history it has no way of knowing', async () => {
    renderTriage({ contacts: 2 })

    const evidence = await screen.findByRole('region', { name: 'Evidence' })
    expect(evidence).toHaveTextContent('Companies you have other contacts at')
    expect(evidence).toHaveTextContent(/not with your own history/i)
    expect(evidence).not.toHaveTextContent(/you both worked/i)
  })

  it('is reachable at /triage through the app shell', async () => {
    mockFetch(createFakeBackend({ contacts: 3 }).handler)

    await renderApp('/triage')

    expect(await screen.findByTestId('triage-card')).toHaveTextContent('Ada')
  })
})

describe('the counters while a decision is still in flight', () => {
  /**
   * The counters run ahead of the server by exactly one decision's worth, and
   * `pending` says by how much. Both halves of that — adding on the keypress
   * and subtracting when the answer lands — use the same two numbers, so any
   * constant nets to zero and is invisible to a test that waits for the write
   * before it looks. These three assert *during* the round trip, which is the
   * only moment the numbers are the screen's own, and between them they pin
   * both counters to what `netkeeper.crm.triage.progress` computes:
   * `triaged = total - by_state[unknown]`, `remaining = sum(by_state[states])`.
   */
  function press(key: string) {
    fireEvent.keyDown(window, { key })
  }

  it('Untriaged: m takes the contact out of the queue and adds a triage', async () => {
    const { backend } = renderTriage({ contacts: 5, latencyMs: 120 })
    await currentName()
    await new Promise((resolve) => setTimeout(resolve, 400))

    press('m')

    expect(screen.getByTestId('triage-progress')).toHaveTextContent('1 / 5')
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('4 left in this queue')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
  })

  it('Both: s keeps the contact in the queue, so only the triage count moves', async () => {
    const { backend } = renderTriage({ contacts: 5, latencyMs: 120 })
    await currentName()
    fireEvent.click(screen.getByRole('button', { name: 'Both' }))
    await waitFor(async () => expect(await currentName()).toContain('Ada'))
    await new Promise((resolve) => setTimeout(resolve, 400))

    press('s')

    // `skip` is one of the states this queue holds, so nobody left it.
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('1 / 5')
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('5 left in this queue')
    await waitFor(() => expect(backend.byId(1).met).toBe('skip'))
  })

  it('a decision that only replaces another moves neither counter', async () => {
    const { backend } = renderTriage({ contacts: 5, latencyMs: 120 })
    await currentName()
    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    await new Promise((resolve) => setTimeout(resolve, 400))

    press('ArrowLeft')
    press('n')

    // Already triaged and already out of the queue, so changing the answer
    // adds no triage and takes nobody off the count of what is left.
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('1 / 5')
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('4 left in this queue')
    await waitFor(() => expect(backend.byId(1).met).toBe('not_met'))
  })
})

describe('the fake backend answers the way the service does', () => {
  it('drops an archived contact out of total and triaged, as progress does', async () => {
    // `netkeeper.crm.triage.progress` groups over the live contacts only, so
    // archiving somebody you had marked met takes them off both counts. The
    // fake used to count every row, and this header was asserted against
    // numbers the API does not produce.
    const backend = createFakeBackend({ contacts: 5 })
    renderTriage({ backend })
    await currentName()

    fireEvent.keyDown(window, { key: 'm' })
    await waitFor(() => expect(screen.getByTestId('triage-progress')).toHaveTextContent('1 / 5'))

    backend.archive(1)
    // `→` writes nothing but its answer carries fresh counters.
    fireEvent.keyDown(window, { key: 'ArrowRight' })

    await waitFor(() => expect(screen.getByTestId('triage-progress')).toHaveTextContent('0 / 4'))
    expect(screen.getByTestId('triage-progress')).toHaveTextContent('4 left in this queue')
  })
})
