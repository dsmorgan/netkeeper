/**
 * Every call the inbox makes, through the generated client (P3-11b).
 *
 * `GET /inbox` lists what reply detection found (spec 11.7); `PUT
 * /inbox/{id}/handled` marks one item handled or not. A note is the contact's
 * own `note` interaction, so it shows on the contact's timeline.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

type Schemas = components['schemas']

export type InboxItem = Schemas['InboxItemOut']
export type InboxPage = Schemas['InboxPageOut']
export type InboxKind = Schemas['InboxKind']

/** Which items the list shows: the unhandled ones, or every one. */
export type HandledFilter = 'unhandled' | 'all'

export const INBOX_PAGE = 25

function fail(status: number, body: unknown, fallback: string): never {
  throw new Error(detailMessage(body) ?? `${fallback} (HTTP ${status})`)
}

export const inboxKeys = {
  all: ['inbox'] as const,
}

export interface InboxFilters {
  handled: HandledFilter
  kind: InboxKind | ''
  enrollment?: number
  offset: number
  /** Items per page; one is enough for a count. */
  limit?: number
}

export function inboxQuery({
  handled,
  kind,
  enrollment,
  offset,
  limit = INBOX_PAGE,
}: InboxFilters) {
  return queryOptions({
    queryKey: [...inboxKeys.all, handled, kind, enrollment ?? null, offset, limit] as const,
    queryFn: async ({ signal }): Promise<InboxPage> => {
      const { data, error, response } = await api.GET('/api/v1/inbox', {
        params: {
          query: {
            limit,
            offset,
            ...(handled === 'unhandled' ? { handled: false } : {}),
            ...(kind === '' ? {} : { kind }),
            ...(enrollment === undefined ? {} : { enrollment_id: enrollment }),
          },
        },
        signal,
      })
      if (data === undefined) fail(response.status, error, 'could not load the inbox')
      return data
    },
  })
}

export async function setHandled(id: number, handled: boolean): Promise<InboxItem> {
  const { data, error, response } = await api.PUT('/api/v1/inbox/{message_id}/handled', {
    params: { path: { message_id: id } },
    body: { handled },
  })
  if (data === undefined) fail(response.status, error, 'could not update the item')
  return data
}

/** A `note` interaction on the contact, dated now. */
export async function addNote(contactId: number, summary: string): Promise<void> {
  const { data, error, response } = await api.POST('/api/v1/contacts/{contact_id}/interactions', {
    params: { path: { contact_id: contactId } },
    body: { kind: 'note', at: new Date().toISOString(), summary },
  })
  if (data === undefined) fail(response.status, error, 'could not add the note')
}
