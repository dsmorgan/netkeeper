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
