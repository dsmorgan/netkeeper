import { describe, expect, it, vi } from 'vitest'

import { ApiError as CrmApiError } from '@/features/crm/api'
import { ApiFailure } from '@/features/contacts/api'
import { ApiError as ImportsApiError } from '@/features/imports/api'
import { ApiError as LinkedinApiError } from '@/features/linkedin/api'
import { TriageError } from '@/features/triage/api'

import { MAX_QUERY_RETRIES, createQueryClient, shouldRetryQuery } from './query-client'

describe('shouldRetryQuery', () => {
  it('allows two retries, pinned', () => {
    expect(MAX_QUERY_RETRIES).toBe(2)
  })

  it.each([400, 401, 403, 404, 409, 413, 422, 429])('never retries a %i', (status) => {
    expect(shouldRetryQuery(0, new ImportsApiError('refused', status))).toBe(false)
  })

  it.each([500, 502, 503, 504])('retries a %i until the cap', (status) => {
    const error = new ImportsApiError('down', status)
    expect(shouldRetryQuery(0, error)).toBe(true)
    expect(shouldRetryQuery(1, error)).toBe(true)
    expect(shouldRetryQuery(2, error)).toBe(false)
  })

  it('retries a network failure, which carries no status', () => {
    expect(shouldRetryQuery(0, new TypeError('Failed to fetch'))).toBe(true)
    expect(shouldRetryQuery(2, new TypeError('Failed to fetch'))).toBe(false)
  })

  it("reads every feature's error class, not just one", () => {
    const refusals = [
      new CrmApiError('gone', 404, null),
      new ApiFailure(404, null, 'contact: gone'),
      new ImportsApiError('gone', 404),
      new LinkedinApiError('gone', 404),
      new TriageError(404, 'gone'),
    ]
    for (const error of refusals) expect(shouldRetryQuery(0, error)).toBe(false)
  })
})

describe('createQueryClient', () => {
  it('asks once when the backend answers 404', async () => {
    const client = createQueryClient()
    const queryFn = vi.fn(() => Promise.reject(new ImportsApiError('no import run 999999', 404)))

    const started = Date.now()
    await expect(client.fetchQuery({ queryKey: ['run', 999999], queryFn })).rejects.toThrow(
      'no import run 999999',
    )
    expect(queryFn).toHaveBeenCalledTimes(1)
    // The old default waited 1s + 2s + 4s before reporting this.
    expect(Date.now() - started).toBeLessThan(500)
  })

  it('asks three times in all when the backend answers 503', async () => {
    const client = createQueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    const queryFn = vi.fn(() => Promise.reject(new ImportsApiError('unavailable', 503)))

    await expect(client.fetchQuery({ queryKey: ['down'], queryFn })).rejects.toThrow()
    expect(queryFn).toHaveBeenCalledTimes(1 + MAX_QUERY_RETRIES)
  })

  it("lets a test's overrides win over the app defaults", async () => {
    const client = createQueryClient({ defaultOptions: { queries: { retry: false } } })
    const queryFn = vi.fn(() => Promise.reject(new ImportsApiError('unavailable', 503)))

    await expect(client.fetchQuery({ queryKey: ['down'], queryFn })).rejects.toThrow()
    expect(queryFn).toHaveBeenCalledTimes(1)
  })
})
