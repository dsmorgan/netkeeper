/**
 * The cap on rows picked one by one (#88).
 *
 * Picks survive paging, so without a cap six pages of 200 sent 1,200 ids to an
 * API that takes 1,000, and the refusal read "count: 422". The real cap is
 * pinned against the OpenAPI export; the behavior is tested at a cap of three,
 * so the test clicks three rows rather than a thousand.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import openapiText from '../../openapi.json?raw'

import { contactPage, contactRow, mockApi } from './contacts-fixtures'
import { jsonResponse } from './fetch'
import { renderApp } from './render'

vi.mock('@/features/contacts/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/features/contacts/api')>()),
  MAX_BULK_IDS: 3,
}))

describe('the pick limit', () => {
  it('matches the API’s own cap on ids', async () => {
    const { MAX_BULK_IDS } =
      await vi.importActual<typeof import('@/features/contacts/api')>('@/features/contacts/api')
    const openapi = JSON.parse(openapiText) as {
      components: {
        schemas: Record<string, { properties: Record<string, { anyOf?: { maxItems?: number }[] }> }>
      }
    }
    const ids = openapi.components.schemas.BulkSelection?.properties.ids
    const cap = ids?.anyOf?.find((option) => option.maxItems !== undefined)?.maxItems
    expect(cap).toBe(1000)
    expect(MAX_BULK_IDS).toBe(cap)
  })

  it('refuses a pick past the cap, says why, and still offers the whole filter', async () => {
    mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/query') {
        return jsonResponse(
          contactPage(
            [1, 2, 3, 4].map((id) => contactRow(id)),
            40,
          ),
        )
      }
      return undefined
    })
    await renderApp('/contacts')
    const boxes = await screen.findAllByRole('checkbox', { name: /^Select (?!every)/ })
    expect(boxes).toHaveLength(4)

    for (const box of boxes.slice(0, 3)) fireEvent.click(box)
    const bar = within(await screen.findByRole('region', { name: 'Bulk actions' }))
    await waitFor(() => expect(bar.getByText('3 selected')).toBeInTheDocument())
    expect(bar.getByRole('status')).toHaveTextContent('3 is the most you can pick one by one')

    fireEvent.click(boxes[3] as HTMLElement)
    expect(bar.getByText('3 selected')).toBeInTheDocument()
    expect(boxes[3]).not.toBeChecked()
    expect(bar.getByRole('button', { name: 'Select all 40 matching this filter' })).toBeEnabled()

    // Un-picking one makes room again.
    fireEvent.click(boxes[0] as HTMLElement)
    await waitFor(() => expect(bar.getByText('2 selected')).toBeInTheDocument())
    expect(bar.queryByRole('status')).toBeNull()
  })
})
