/**
 * Fixtures for the Contacts screens.
 *
 * Every person, company, address, and number here is invented. Nothing from a
 * real export goes into a test (CLAUDE.md), and no test touches the network:
 * `mockApi` answers from memory and records what was asked.
 */

import type { components } from '@/api/schema'

import { jsonResponse, mockFetch } from './fetch'

type Schemas = components['schemas']
export type ContactRow = Schemas['ContactRow']
export type ContactDetail = Schemas['ContactDetail']
export type ContactPage = Schemas['ContactPage']

const FIRST_NAMES = ['Ada', 'Bo', 'Cleo', 'Dax', 'Esme', 'Fen', 'Gita', 'Huw']
const LAST_NAMES = ['Ventura', 'Quill', 'Marsh', 'Okonjo', 'Rask', 'Delacroix']
const COMPANIES = ['Tessellate Labs', 'Northwind Ferry', 'Pellucid Foods', 'Grebe Analytics']

export function contactRow(id: number, overrides: Partial<ContactRow> = {}): ContactRow {
  const first = FIRST_NAMES[id % FIRST_NAMES.length] as string
  const last = LAST_NAMES[id % LAST_NAMES.length] as string
  return {
    id,
    first_name: first,
    last_name: last,
    preferred_name: first,
    headline: `Builds things at ${COMPANIES[id % COMPANIES.length]}`,
    current_title: 'Staff Engineer',
    current_company: COMPANIES[id % COMPANIES.length] as string,
    location: 'Aberdare, Wales',
    connected_on: '2024-03-01',
    met: 'unknown',
    do_not_contact: false,
    archived_at: null,
    li_url: `https://www.linkedin.com/in/person-${id}`,
    last_contacted_at: null,
    primary_email: `person-${id}@example.test`,
    primary_phone: null,
    ...overrides,
  }
}

export function contactPage(
  items: ContactRow[],
  total = items.length,
  describe = 'every live contact',
): ContactPage {
  return { items, total, describe }
}

export function contactDetail(overrides: Partial<ContactDetail> = {}): ContactDetail {
  return {
    id: 1,
    li_urn: 'urn:li:fsd_profile:FAKE-0001',
    li_public_id: 'ada-ventura-fake',
    li_url: 'https://www.linkedin.com/in/ada-ventura-fake',
    first_name: 'Ada',
    last_name: 'Ventura',
    preferred_name: 'Ada',
    headline: 'Principal Engineer at Tessellate Labs',
    current_title: 'Principal Engineer',
    current_company: 'Tessellate Labs',
    location: 'Aberdare, Wales',
    connected_on: '2024-03-01',
    degree: 1,
    met: 'unknown',
    triaged_at: null,
    do_not_contact: false,
    do_not_contact_reason: null,
    li_missing_count: 0,
    li_disconnected_at: null,
    last_enriched_at: null,
    enrich_priority: 0,
    last_contacted_at: null,
    notes: null,
    archived_at: null,
    source: 'sync',
    created_at: '2026-01-02T09:00:00Z',
    updated_at: '2026-01-02T09:00:00Z',
    merged_into_id: null,
    emails: [],
    phones: [],
    links: [],
    positions: [],
    snapshots: [],
    timeline: [],
    field_sources: {},
    synced_values: {},
    overridden_fields: [],
    resolved_from: null,
    ...overrides,
  }
}

export interface SeenRequest {
  method: string
  path: string
  body: unknown
}

export type ApiHandler = (
  request: Request,
  body: unknown,
) => Response | undefined | Promise<Response | undefined>

/**
 * Routes every fetch to `handler`, answering `/health` and `/me` itself so the
 * app shell renders. Returns the list it records requests into.
 */
export function mockApi(handler: ApiHandler): SeenRequest[] {
  const seen: SeenRequest[] = []
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    const raw = request.method === 'GET' ? '' : await request.clone().text()
    const body: unknown = raw === '' ? undefined : JSON.parse(raw)
    seen.push({ method: request.method, path: pathname, body })

    const answer = await handler(request, body)
    if (answer) return answer

    if (pathname === '/api/v1/health') return jsonResponse({ status: 'ok', version: '0.0.1-test' })
    if (pathname === '/api/v1/me') {
      return jsonResponse({
        id: 1,
        kind: 'local',
        display_name: 'Test User',
        email: null,
        timezone: 'UTC',
      })
    }
    if (pathname === '/api/v1/tags') return jsonResponse([])
    return new Response('not found', { status: 404 })
  })
  return seen
}

/** The `POST /contacts/query` bodies the table sent, oldest first. */
export function queries(seen: readonly SeenRequest[]): Schemas['ContactQuery'][] {
  return seen
    .filter((entry) => entry.path === '/api/v1/contacts/query')
    .map((entry) => entry.body as Schemas['ContactQuery'])
}

export function lastQuery(seen: readonly SeenRequest[]): Schemas['ContactQuery'] {
  const all = queries(seen)
  const last = all[all.length - 1]
  if (!last) throw new Error('the table sent no query')
  return last
}
