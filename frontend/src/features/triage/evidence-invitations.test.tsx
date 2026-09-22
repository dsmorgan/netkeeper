/**
 * What the card says about invitations, which are not messages.
 *
 * The importer writes an invitation as the same kind of row as a message, and
 * the batches have always left them out: clicking Connect is not a
 * conversation. The panel used to count them together, so a contact whose
 * whole history was one invitation read "1 message" over a line that said
 * otherwise. These are the three shapes that line can take.
 */

import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { createFakeBackend } from './test-backend'
import { currentName, renderTriage } from './test-render'

async function panelFor(options: { messages: number; invitations: number }) {
  const backend = createFakeBackend({ contacts: 2, withMessages: options.messages > 0 ? 1 : 0 })
  backend.byId(1).invitations = options.invitations
  renderTriage({ backend })
  await currentName()
  const heading = await screen.findByRole('heading', { name: 'Messages' })
  const panel = heading.parentElement
  if (panel === null) throw new Error('the messages block has no container')
  return panel
}

describe('invitations on the card', () => {
  it('says nothing about invitations when there are none', async () => {
    const panel = await panelFor({ messages: 0, invitations: 0 })
    expect(panel).toHaveTextContent('No message history')
    expect(panel).not.toHaveTextContent(/invitation/i)
  })

  it('names one invitation, and does not call it a message', async () => {
    const panel = await panelFor({ messages: 0, invitations: 1 })
    expect(panel).toHaveTextContent('No message history')
    expect(panel).toHaveTextContent('1 invitation is on file, under the timeline')
    expect(panel).not.toHaveTextContent('1 message ·')
  })

  it('counts several invitations in the plural', async () => {
    const panel = await panelFor({ messages: 0, invitations: 3 })
    expect(panel).toHaveTextContent('3 invitations are on file')
  })

  it('keeps the invitations beside the messages when there are both', async () => {
    const panel = await panelFor({ messages: 1, invitations: 2 })
    expect(panel).toHaveTextContent('2 messages · 2 in, 0 out')
    expect(panel).toHaveTextContent('2 invitations besides, not counted here')
  })
})
