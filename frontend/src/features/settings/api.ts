/**
 * Every call the Settings page makes, through the generated client.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

export type Posture = components['schemas']['PostureOut']
export type Protection = components['schemas']['ProtectionOut']
export type DoNotSendEntry = components['schemas']['DoNotSendOut']

/**
 * Every protection the LinkedIn extractor has (spec section 9, P2-11), read-only.
 *
 * Built with no browser probe (`GET /posture` never attaches, CLAUDE.md), so
 * the `linkedin session` row always reads "unknown" here — a live check stays
 * `netkeeper preflight`, a terminal command.
 */
export const postureQuery = queryOptions({
  queryKey: ['posture'] as const,
  queryFn: async ({ signal }): Promise<Posture> => {
    const { data, error, response } = await api.GET('/api/v1/posture', { signal })
    if (data === undefined) {
      const detail = (error as { detail?: unknown } | undefined)?.detail
      const message = typeof detail === 'string' && detail !== '' ? detail : null
      throw new Error(message ?? `GET /api/v1/posture returned ${response.status}`)
    }
    return data
  },
})

function failure(error: unknown, response: Response, what: string): Error {
  return new Error(detailMessage(error) ?? `${what} returned ${response.status}`)
}

/** The addresses no campaign sends to (#238), newest first. */
export const doNotSendQuery = queryOptions({
  queryKey: ['do-not-send'] as const,
  queryFn: async ({ signal }): Promise<DoNotSendEntry[]> => {
    const { data, error, response } = await api.GET('/api/v1/do-not-send', { signal })
    if (data === undefined) throw failure(error, response, 'GET /do-not-send')
    return data
  },
})

/** Take an address off the list: a person's explicit action, so campaigns may send to it. */
export async function removeDoNotSend(entryId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/do-not-send/{entry_id}', {
    params: { path: { entry_id: entryId } },
  })
  if (!response.ok) throw failure(error, response, 'DELETE /do-not-send/{id}')
}
