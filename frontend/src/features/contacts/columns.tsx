/**
 * The Contacts table's columns: what each one is called, what the server has to
 * send for it, whether it sorts, and how the cell renders (spec 10.1).
 *
 * `requires` is what makes the column picker cheap: a query asks for the scalar
 * columns the chosen columns need and nothing else, so hiding `notes` stops it
 * crossing the wire.
 */

import { Link } from '@tanstack/react-router'
import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'

import { displayName, formatDate, formatDateTime } from './format'
import type { ContactColumn, ContactRow, SortField } from './types'
import { MET_LABELS } from './types'

export type ColumnId = ContactColumn | 'name' | 'primary_email' | 'primary_phone'

export interface ColumnSpec {
  id: ColumnId
  label: string
  /** The scalar columns the server must return for this cell. */
  requires: readonly ContactColumn[]
  /** The key this header sorts by; a column the API cannot sort on has none. */
  sort?: SortField
  render: (row: ContactRow) => ReactNode
  /** A wider cell that may be clipped rather than pushing the table sideways. */
  wide?: boolean
}

/** An em dash for a cell with nothing in it. A constant, not a component: this
 * module exports column specs, and a component here would break fast refresh. */
const DASH: ReactNode = <span className="text-muted-foreground">—</span>

function plain(value: string | number | null | undefined): ReactNode {
  return value === null || value === undefined || value === '' ? DASH : value
}

function textColumn(
  id: ContactColumn,
  label: string,
  options: { sort?: SortField; wide?: boolean } = {},
): ColumnSpec {
  return {
    id,
    label,
    requires: [id],
    ...options,
    render: (row) => plain(row[id] as string | number | null | undefined),
  }
}

function dateColumn(id: ContactColumn, label: string, sort?: SortField): ColumnSpec {
  return {
    id,
    label,
    requires: [id],
    sort,
    render: (row) => plain(formatDate(row[id] as string | null | undefined)),
  }
}

function timeColumn(id: ContactColumn, label: string, sort?: SortField): ColumnSpec {
  return {
    id,
    label,
    requires: [id],
    sort,
    render: (row) => plain(formatDateTime(row[id] as string | null | undefined)),
  }
}

const NAME_COLUMN: ColumnSpec = {
  id: 'name',
  label: 'Name',
  requires: ['first_name', 'last_name', 'preferred_name'],
  sort: 'last_name',
  render: (row) => (
    <Link
      to="/contacts/$contactId"
      params={{ contactId: String(row.id) }}
      className="font-medium text-foreground underline-offset-4 hover:underline"
    >
      {displayName(row)}
    </Link>
  ),
}

const MET_COLUMN: ColumnSpec = {
  id: 'met',
  label: 'Met',
  requires: ['met'],
  sort: 'met',
  render: (row) =>
    row.met ? (
      <Badge variant={row.met === 'met' ? 'default' : 'outline'}>{MET_LABELS[row.met]}</Badge>
    ) : (
      DASH
    ),
}

const DNC_COLUMN: ColumnSpec = {
  id: 'do_not_contact',
  label: 'Do not contact',
  requires: ['do_not_contact'],
  sort: 'do_not_contact',
  render: (row) =>
    row.do_not_contact ? <Badge variant="destructive">Do not contact</Badge> : DASH,
}

const LI_URL_COLUMN: ColumnSpec = {
  id: 'li_url',
  label: 'LinkedIn URL',
  requires: ['li_url'],
  wide: true,
  render: (row) =>
    row.li_url ? (
      <a
        href={row.li_url}
        target="_blank"
        rel="noreferrer noopener"
        className="underline-offset-4 hover:underline"
      >
        {row.li_url.replace(/^https?:\/\/(www\.)?/, '')}
      </a>
    ) : (
      DASH
    ),
}

/** Every column the picker offers, in the order it offers them. */
export const COLUMNS: readonly ColumnSpec[] = [
  NAME_COLUMN,
  textColumn('preferred_name', 'Preferred name', { sort: 'preferred_name' }),
  textColumn('first_name', 'First name', { sort: 'first_name' }),
  textColumn('last_name', 'Last name', { sort: 'last_name' }),
  textColumn('headline', 'Headline', { sort: 'headline', wide: true }),
  textColumn('current_title', 'Title', { sort: 'current_title' }),
  textColumn('current_company', 'Company', { sort: 'current_company' }),
  textColumn('location', 'Location', { sort: 'location' }),
  { id: 'primary_email', label: 'Email', requires: [], render: (row) => plain(row.primary_email) },
  { id: 'primary_phone', label: 'Phone', requires: [], render: (row) => plain(row.primary_phone) },
  dateColumn('connected_on', 'Connected on', 'connected_on'),
  textColumn('degree', 'Degree', { sort: 'degree' }),
  MET_COLUMN,
  timeColumn('triaged_at', 'Triaged', 'triaged_at'),
  DNC_COLUMN,
  textColumn('do_not_contact_reason', 'Do-not-contact reason', { wide: true }),
  timeColumn('last_contacted_at', 'Last contacted', 'last_contacted_at'),
  timeColumn('last_enriched_at', 'Last enriched', 'last_enriched_at'),
  textColumn('enrich_priority', 'Enrich priority'),
  textColumn('li_public_id', 'LinkedIn id', { sort: 'li_public_id' }),
  LI_URL_COLUMN,
  textColumn('li_urn', 'LinkedIn URN'),
  textColumn('li_missing_count', 'Missing from LinkedIn'),
  timeColumn('li_disconnected_at', 'Disconnected', 'li_disconnected_at'),
  textColumn('notes', 'Notes', { wide: true }),
  textColumn('source', 'Source', { sort: 'source' }),
  timeColumn('archived_at', 'Archived', 'archived_at'),
  timeColumn('created_at', 'Created', 'created_at'),
  timeColumn('updated_at', 'Updated', 'updated_at'),
]

export const COLUMNS_BY_ID = new Map(COLUMNS.map((column) => [column.id, column]))

export const DEFAULT_COLUMNS: readonly ColumnId[] = [
  'name',
  'headline',
  'current_company',
  'current_title',
  'location',
  'connected_on',
  'met',
  'last_contacted_at',
]

/**
 * Scalars every query asks for whatever is on screen: the row actions need a
 * name to confirm with, a URL to open, and the current met and do-not-contact
 * values to toggle from.
 */
const ALWAYS: readonly ContactColumn[] = [
  'first_name',
  'last_name',
  'preferred_name',
  'li_url',
  'met',
  'do_not_contact',
  'archived_at',
]

/** The `columns` a query needs for `visible`, deduplicated. */
export function requestedColumns(visible: readonly ColumnId[]): ContactColumn[] {
  const wanted = new Set<ContactColumn>(ALWAYS)
  for (const id of visible) {
    for (const column of COLUMNS_BY_ID.get(id)?.requires ?? []) wanted.add(column)
  }
  return [...wanted]
}

/** Drops ids no longer in the registry, so a stale saved view still renders. */
export function knownColumns(ids: readonly string[]): ColumnId[] {
  return ids.filter((id): id is ColumnId => COLUMNS_BY_ID.has(id as ColumnId))
}
