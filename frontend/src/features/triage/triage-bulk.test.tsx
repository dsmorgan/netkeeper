/**
 * The bulk suggestion banner (spec 10.2).
 *
 * The count on the banner is a preview, and the apply sends it back. When the
 * set has moved the API refuses with `409` and writes nothing — which is not a
 * failure to report but a preview to take again, and that is what this asserts.
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { currentName, renderTriage } from './test-render'

describe('the bulk suggestion', () => {
  it('is not drawn when it matches nobody', async () => {
    renderTriage({ contacts: 4, withMessages: 0 })
    await currentName()

    await waitFor(() => expect(screen.queryByTestId('bulk-suggestion')).not.toBeInTheDocument())
  })

  it('previews with a count and applies as one batch', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()

    const banner = await screen.findByTestId('bulk-suggestion')
    expect(banner).toHaveTextContent('You have message threads with 3 untriaged people.')

    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(backend.byId(2).met).toBe('met')
    expect(backend.byId(3).met).toBe('met')
    expect(backend.byId(4).met).toBe('unknown')
    expect(await screen.findByRole('status')).toHaveTextContent(/Marked 3 contacts as met/)
    // One batch id, so one undo takes it all back.
    const batches = new Set(backend.decisions.map((decision) => decision.batch_id))
    expect(batches.size).toBe(1)
  })

  it('goes away once everyone it matched has an answer', async () => {
    renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))

    await waitFor(() => expect(screen.queryByTestId('bulk-suggestion')).not.toBeInTheDocument())
  })

  it('re-previews instead of erroring when the count moved', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await screen.findByRole('button', { name: 'Mark 3 as met' })

    // A fourth person gains message history after the banner was drawn.
    backend.setMessageCount(4, 2)
    fireEvent.click(screen.getByRole('button', { name: 'Mark 3 as met' }))

    // Nothing was applied, and the banner comes back with the count as it is.
    expect(await screen.findByRole('button', { name: 'Mark 4 as met' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/nothing was applied/i)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    for (const id of [1, 2, 3, 4]) expect(backend.byId(id).met).toBe('unknown')

    // Accepting the new count applies it.
    fireEvent.click(screen.getByRole('button', { name: 'Mark 4 as met' }))
    await waitFor(() => expect(backend.byId(4).met).toBe('met'))
  })

  it('is taken back whole by one undo', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))
    await waitFor(() => expect(backend.byId(3).met).toBe('met'))

    fireEvent.keyDown(window, { key: 'u' })

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    expect(backend.byId(2).met).toBe('unknown')
    expect(backend.byId(3).met).toBe('unknown')
    expect(await screen.findByTestId('bulk-suggestion')).toHaveTextContent('3 untriaged people')
  })

  it('moves the queue on when the batch empties the front of it', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    expect(await currentName()).toContain('Ada')

    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))

    await waitFor(() => expect(backend.byId(3).met).toBe('met'))
    await waitFor(async () => expect(await currentName()).toContain('Dev'))
  })
})
