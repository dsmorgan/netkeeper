/**
 * Fixtures and a stand-in backend for the import tests.
 *
 * Only the test files import this. Everyone in it is invented: `.example`
 * addresses and reserved `555-01xx` numbers, never a real person (CLAUDE.md).
 */
import { jsonResponse } from '@/test/fetch'

import type { ImportRow, ImportRun, Inspection, PresetList, PreviewRow } from './types'

export const HEADERS = [
  'First Name',
  'Last Name',
  'Email Address',
  'Company',
  'Position',
  'Connected On',
  'Notes',
]

// The preamble line is quoted, as LinkedIn writes it. Unquoted, its comma makes
// the real parser take *that* line as the header and answer 422 — checked
// against `parse_csv`, not assumed.
export const CSV_TEXT = [
  'Notes:',
  '"When exporting your connection data, your recipient information is included."',
  '',
  HEADERS.join(','),
  'Rosalind,Quillfeather,rosalind@nimbus-kettle.example,Nimbus Kettle Co,Kettle Fitter,01 Mar 2021,',
  'Tobias,Marrowbone,tobias@orrery-works.example,Orrery Works,Orrery Wright,14 Jun 2022,',
  'Imogen,Pallisade,imogen@orrery-works.example,Orrery Works,Escapement Lead,02 Feb 2023,',
  ',,,,,,stray note',
].join('\n')

export const PRESETS: PresetList = {
  builtin: [
    { name: 'linkedin-archive', builtin: true, mapping: { 'First Name': 'first_name' } },
    { name: 'nine-column', builtin: true, mapping: { 'First Name': 'first_name' } },
    { name: 'linkedhelper', builtin: true, mapping: { 'Profile Url': 'li_url' } },
  ],
  saved: [{ name: 'my-crm-export', builtin: false, mapping: { Company: 'current_company' } }],
}

export const ARCHIVE_INSPECTION: Inspection = {
  headers: HEADERS,
  row_count: 4,
  preamble_rows: 3,
  detected_preset: 'linkedin-archive',
  preset: 'linkedin-archive',
  mapping: {
    'First Name': 'first_name',
    'Last Name': 'last_name',
    'Email Address': 'email',
    Company: 'current_company',
    Position: 'current_title',
    'Connected On': 'connected_on',
  },
  unmapped: ['Notes'],
  sample: [
    {
      'First Name': 'Rosalind',
      'Last Name': 'Quillfeather',
      'Email Address': 'rosalind@nimbus-kettle.example',
      Company: 'Nimbus Kettle Co',
      Position: 'Kettle Fitter',
      'Connected On': '01 Mar 2021',
      Notes: '',
    },
  ],
}

/** A CSV no built-in preset recognizes: every column waiting to be mapped by hand. */
export const UNRECOGNIZED_INSPECTION: Inspection = {
  headers: ['Given', 'Surname', 'Mail'],
  row_count: 2,
  preamble_rows: 0,
  detected_preset: null,
  preset: null,
  mapping: {},
  unmapped: ['Given', 'Surname', 'Mail'],
  sample: [{ Given: 'Hortensia', Surname: 'Blennerhassett', Mail: 'h@tarnish.example' }],
}

/** The same file read with a preset that claims fewer of its columns. */
export const NINE_COLUMN_INSPECTION: Inspection = {
  ...ARCHIVE_INSPECTION,
  detected_preset: 'linkedin-archive',
  preset: 'nine-column',
  mapping: {
    'First Name': 'first_name',
    'Last Name': 'last_name',
    'Email Address': 'email',
  },
  unmapped: ['Company', 'Position', 'Connected On', 'Notes'],
}

export const DRAFT_RUN: ImportRun = {
  id: 7,
  source_kind: 'csv',
  filename: 'connections.csv',
  preset: 'linkedin-archive',
  mapping: ARCHIVE_INSPECTION.mapping,
  status: 'draft',
  total_rows: 4,
  matched_count: 1,
  created_count: 1,
  candidate_count: 1,
  skipped_count: 1,
  tagged_contacts: 0,
  tags_added: 0,
  tags_removed: 0,
  committed_at: null,
  rolled_back_at: null,
  created_at: '2026-09-20T10:00:00Z',
  updated_at: '2026-09-20T10:00:00Z',
}

export const COMMITTED_RUN: ImportRun = {
  ...DRAFT_RUN,
  status: 'committed',
  created_count: 2,
  candidate_count: 1,
  committed_at: '2026-09-20T10:05:00Z',
  updated_at: '2026-09-20T10:05:00Z',
}

const RAW_ROSALIND = ARCHIVE_INSPECTION.sample[0] as Record<string, string>

const RAW_IMOGEN = {
  'First Name': 'Imogen',
  'Last Name': 'Pallisade',
  'Email Address': 'imogen@orrery-works.example',
  Company: 'Orrery Works',
  Position: 'Escapement Lead',
  'Connected On': '02 Feb 2023',
  Notes: '',
}

export const PREVIEW_ROWS: PreviewRow[] = [
  {
    row_number: 1,
    raw: RAW_ROSALIND,
    resolution: 'matched',
    contact_id: 11,
    matched_by: 'email',
    candidate_ids: [],
    changes: [
      {
        field: 'current_title',
        before: 'Kettle Fitter',
        after: 'Senior Kettle Fitter',
        refused: false,
        kept_source: null,
      },
      {
        field: 'current_company',
        before: 'Nimbus Kettle Co',
        after: 'Nimbus Kettle Works',
        refused: true,
        kept_source: 'manual',
      },
    ],
    problem: null,
  },
  {
    row_number: 2,
    raw: {
      'First Name': 'Tobias',
      'Last Name': 'Marrowbone',
      'Email Address': 'tobias@orrery-works.example',
      Company: 'Orrery Works',
      Position: 'Orrery Wright',
      'Connected On': '14 Jun 2022',
      Notes: '',
    },
    resolution: 'created',
    contact_id: null,
    matched_by: null,
    candidate_ids: [],
    changes: [
      { field: 'first_name', before: null, after: 'Tobias', refused: false, kept_source: null },
    ],
    // A cell dropped for a reason, on a row that still lands (P1-04 review).
    problem: "Provider Id: 'ab12cd' is not a LinkedIn URN (urn:li:...)",
  },
  {
    row_number: 3,
    raw: RAW_IMOGEN,
    resolution: 'candidate',
    contact_id: null,
    matched_by: null,
    candidate_ids: [12, 13],
    changes: [
      {
        field: 'current_title',
        before: 'Escapement Fitter',
        after: 'Escapement Lead',
        refused: false,
        kept_source: null,
      },
    ],
    problem: null,
  },
  {
    row_number: 4,
    raw: { 'First Name': '', Notes: 'stray note' },
    resolution: 'skipped',
    contact_id: null,
    matched_by: null,
    candidate_ids: [],
    changes: [],
    problem: 'no column identifies anybody',
  },
]

export const CANDIDATE_ROW: ImportRow = {
  id: 103,
  row_number: 3,
  raw: RAW_IMOGEN,
  resolution: 'candidate',
  contact_id: null,
  matched_by: null,
  candidate_ids: [12, 13],
  decision: null,
  refused: [],
  error: null,
}

export const COMMITTED_ROWS: ImportRow[] = [
  {
    id: 101,
    row_number: 1,
    raw: RAW_ROSALIND,
    resolution: 'matched',
    contact_id: 11,
    matched_by: 'email',
    decision: null,
    candidate_ids: [],
    refused: [
      {
        field: 'current_company',
        incoming: 'Nimbus Kettle Works',
        kept: 'Nimbus Kettle Co',
        source: 'manual',
      },
    ],
    error: null,
  },
  {
    id: 102,
    row_number: 2,
    raw: {
      'First Name': 'Tobias',
      'Last Name': 'Marrowbone',
      'Email Address': 'tobias@orrery-works.example',
    },
    resolution: 'created',
    contact_id: 31,
    matched_by: null,
    decision: null,
    candidate_ids: [],
    refused: [],
    error: null,
  },
]

export interface Call {
  method: string
  path: string
  query: URLSearchParams
  body: unknown
}

type Handler = (call: Call) => Response | Promise<Response>

const RESOLUTIONS = ['matched', 'created', 'candidate', 'skipped']

/**
 * The query bounds the real routes declare, so the fakes refuse what the API
 * refuses.
 *
 * Keying a fake on method and path alone let three contract mistakes through
 * unnoticed — a preview limit of 999, a rows limit of 5000, and a resolution
 * value that is not one of the four — each of which the real API answers 422
 * to, and each of which would blank the screen.
 */
const QUERY_RULES: ReadonlyArray<{
  path: RegExp
  limit: readonly [number, number]
  resolution?: boolean
}> = [
  { path: /^\/api\/v1\/imports$/, limit: [1, 200] },
  { path: /^\/api\/v1\/imports\/\d+\/preview$/, limit: [1, 200] },
  { path: /^\/api\/v1\/imports\/\d+\/rows$/, limit: [1, 500], resolution: true },
]

function queryComplaint(call: Call): string | null {
  const rule = QUERY_RULES.find((candidate) => candidate.path.test(call.path))
  if (rule === undefined) return null
  const limit = call.query.get('limit')
  if (limit !== null) {
    const value = Number(limit)
    if (!Number.isInteger(value) || value < rule.limit[0] || value > rule.limit[1]) {
      return `limit: input should be between ${rule.limit[0]} and ${rule.limit[1]}`
    }
  }
  const offset = call.query.get('offset')
  if (offset !== null && (!Number.isInteger(Number(offset)) || Number(offset) < 0)) {
    return 'offset: input should be greater than or equal to 0'
  }
  const resolution = call.query.get('resolution')
  if (resolution !== null && (!rule.resolution || !RESOLUTIONS.includes(resolution))) {
    return `resolution: input should be ${RESOLUTIONS.join(', ')}`
  }
  return null
}

/**
 * Routes `METHOD /path` to a handler, answers `/health` and `/me` for the app
 * shell, checks the query against what the route declares, and records every
 * call so a test can assert on what was sent.
 */
export function backend(handlers: Record<string, Handler>, calls: Call[] = []) {
  return async (request: Request): Promise<Response> => {
    const url = new URL(request.url)
    const raw = request.method === 'GET' ? '' : await request.text()
    const call: Call = {
      method: request.method,
      path: url.pathname,
      query: url.searchParams,
      body: raw === '' ? undefined : JSON.parse(raw),
    }
    calls.push(call)
    const complaint = queryComplaint(call)
    if (complaint !== null) {
      return jsonResponse({ detail: [{ msg: complaint }] }, 422)
    }
    const handler = handlers[`${request.method} ${url.pathname}`]
    if (handler !== undefined) return handler(call)
    if (url.pathname === '/api/v1/health') {
      return jsonResponse({ status: 'ok', version: '0.0.1-test' })
    }
    if (url.pathname === '/api/v1/me') {
      return jsonResponse({
        id: 1,
        kind: 'local',
        display_name: 'Test User',
        email: null,
        timezone: 'UTC',
      })
    }
    return jsonResponse({ detail: `no fake for ${request.method} ${url.pathname}` }, 404)
  }
}

/** A CSV as the browser would hand it to the wizard. */
export function csvFile(text = CSV_TEXT, name = 'connections.csv'): File {
  return new File([text], name, { type: 'text/csv' })
}

/**
 * A commit that refuses like the real one.
 *
 * `commit` answers 409 while any candidate row has no decision unless it was
 * told to skip them. A fake that always succeeds hides exactly the flow this
 * wizard exists to prevent.
 */
export function commitLike(candidateRows: readonly number[], applied: ImportRun) {
  return (call: Call): Response => {
    const body = (call.body ?? {}) as {
      decisions?: Array<{ row_number: number }>
      skip_undecided?: boolean
    }
    const decided = new Set((body.decisions ?? []).map((decision) => decision.row_number))
    const undecided = candidateRows.filter((row) => !decided.has(row))
    if (undecided.length > 0 && body.skip_undecided !== true) {
      return jsonResponse(
        {
          detail:
            `${undecided.length} row(s) resolve to a candidate and have no decision ` +
            `(rows ${undecided.join(', ')}); decide each one, or commit with skip_undecided`,
        },
        409,
      )
    }
    return jsonResponse(applied)
  }
}
