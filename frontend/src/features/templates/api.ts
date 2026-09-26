/**
 * Every call the template editor makes, through the generated client (P3-10).
 *
 * The `/templates` API is P3-03's: CRUD, preview, and `POST /templates/lint`,
 * which lints text that is not saved yet so an error shows before you save.
 * Lint never blocks a save; it blocks activation.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

export type TemplateOut = components['schemas']['TemplateOut']
export type TemplateChannel = TemplateOut['channel']
export type LintIssue = components['schemas']['LintIssueOut']
export type TemplatePreview = components['schemas']['TemplatePreviewOut']
export type ContactRow = components['schemas']['ContactRow']

export interface TemplateDraft {
  name: string
  channel: TemplateChannel
  subject: string
  body: string
}

/** A request the backend refused, with its status and the server's own sentence. */
export class TemplateApiError extends Error {
  readonly status: number

  constructor(status: number, body: unknown, fallback: string) {
    super(detailMessage(body) ?? `${fallback} (HTTP ${status})`)
    this.name = 'TemplateApiError'
    this.status = status
  }
}

function fail(status: number, body: unknown, fallback: string): never {
  throw new TemplateApiError(status, body, fallback)
}

export const templateKeys = {
  all: ['templates'] as const,
  list: () => [...templateKeys.all, 'list'] as const,
  one: (id: number) => [...templateKeys.all, 'one', id] as const,
  versions: (id: number) => [...templateKeys.all, 'versions', id] as const,
  preview: (id: number, contactId: number) =>
    [...templateKeys.all, 'preview', id, contactId] as const,
}

/** The newest version of every template, by name. */
export const templatesQuery = queryOptions({
  queryKey: templateKeys.list(),
  queryFn: async ({ signal }): Promise<TemplateOut[]> => {
    const { data, error, response } = await api.GET('/api/v1/templates', { signal })
    if (data === undefined) fail(response.status, error, 'could not load the templates')
    return data
  },
})

async function getTemplate(id: number, signal?: AbortSignal): Promise<TemplateOut> {
  const { data, error, response } = await api.GET('/api/v1/templates/{template_id}', {
    params: { path: { template_id: id } },
    signal,
  })
  if (data === undefined) fail(response.status, error, 'could not load the template')
  return data
}

/** How far back the history walks. Far past any real template; stops a cycle cold. */
export const MAX_VERSIONS = 100

/**
 * Every version of the template `id` is the newest of, newest first.
 *
 * Each version names the one it replaced (`previous_id`), so the history is
 * that chain, walked one `GET` at a time. A template gets a new version only
 * when an edit lands while a campaign uses it, so the chain stays short.
 */
export function versionsQuery(id: number) {
  return queryOptions({
    queryKey: templateKeys.versions(id),
    queryFn: async ({ signal }): Promise<TemplateOut[]> => {
      const versions: TemplateOut[] = []
      let next: number | null = id
      while (next !== null && versions.length < MAX_VERSIONS) {
        const row = await getTemplate(next, signal)
        versions.push(row)
        next = row.previous_id
      }
      return versions
    },
  })
}

/** Lint text that is not saved yet, exactly as a save of it would. */
export async function lintDraft(
  draft: Pick<TemplateDraft, 'channel' | 'subject' | 'body'>,
  signal?: AbortSignal,
): Promise<LintIssue[]> {
  const { data, error, response } = await api.POST('/api/v1/templates/lint', {
    body: { channel: draft.channel, subject: draft.subject, body: draft.body },
    signal,
  })
  if (data === undefined) fail(response.status, error, 'could not lint the template')
  return data
}

export async function createTemplate(draft: TemplateDraft): Promise<TemplateOut> {
  const { data, error, response } = await api.POST('/api/v1/templates', { body: draft })
  if (data === undefined) fail(response.status, error, 'could not save the template')
  return data
}

/**
 * Save an edit. The answer is the row that now holds the template: the same
 * one, or a new version when a campaign uses the one you edited (spec 8.5).
 */
export async function updateTemplate(id: number, draft: TemplateDraft): Promise<TemplateOut> {
  const { data, error, response } = await api.PATCH('/api/v1/templates/{template_id}', {
    params: { path: { template_id: id } },
    body: draft,
  })
  if (data === undefined) fail(response.status, error, 'could not save the template')
  return data
}

export async function deleteTemplate(id: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/templates/{template_id}', {
    params: { path: { template_id: id } },
  })
  if (!response.ok) fail(response.status, error, 'could not delete the template')
}

/** One saved version rendered for one contact. Missing contact data is a warning, not a failure. */
export function previewQuery(id: number, contactId: number) {
  return queryOptions({
    queryKey: templateKeys.preview(id, contactId),
    queryFn: async ({ signal }): Promise<TemplatePreview> => {
      const { data, error, response } = await api.GET('/api/v1/templates/{template_id}/preview', {
        params: { path: { template_id: id }, query: { contact_id: contactId } },
        signal,
      })
      if (data === undefined) fail(response.status, error, 'could not render the preview')
      return data
    },
  })
}

/** How many contacts the preview's picker offers for one search. */
export const CONTACT_PICK_LIMIT = 8

/** The Contacts quick search (`GET /contacts?q=`), for picking who to preview for. */
export function contactSearchQuery(q: string) {
  return queryOptions({
    queryKey: ['contacts', 'quick-search', q, CONTACT_PICK_LIMIT] as const,
    queryFn: async ({ signal }): Promise<ContactRow[]> => {
      const { data, error, response } = await api.GET('/api/v1/contacts', {
        params: { query: { q, limit: CONTACT_PICK_LIMIT } },
        signal,
      })
      if (data === undefined) fail(response.status, error, 'could not search the contacts')
      return data.items
    },
  })
}
