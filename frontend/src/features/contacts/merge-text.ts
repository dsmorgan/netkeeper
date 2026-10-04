/**
 * A merge's moves in sentences (#363), for the preview and its confirmation.
 */

import { displayName, formatDate } from './format'
import type { ContactDetail, MergeMoves } from './types'

/**
 * A contact in a merge: its id, its name, and whatever details are known. A
 * detail left `undefined` is unknown (not loaded yet); `null` is known to be empty.
 */
export interface Nameable {
  id: number
  first_name?: string | null
  last_name?: string | null
  preferred_name?: string | null
  primary_email?: string | null
  current_company?: string | null
  li_public_id?: string | null
  created_at?: string | null
}

/** A contact in full as a {@link Nameable}: its primary email is among its emails. */
export function nameableOf(contact: ContactDetail): Nameable {
  const email = contact.emails.find((row) => row.is_primary) ?? contact.emails[0]
  return { ...contact, primary_email: email?.email ?? null }
}

interface Detail {
  read: (contact: Nameable) => string | null | undefined
  show: (value: string | null) => string
}

/** The details that tell two records of one name apart, in the order they are tried. */
const DETAILS: readonly Detail[] = [
  {
    read: (c) =>
      typeof c.primary_email === 'string' ? c.primary_email.toLowerCase() : c.primary_email,
    show: (v) => v ?? 'no email',
  },
  { read: (c) => c.current_company, show: (v) => v ?? 'no company' },
  { read: (c) => c.li_public_id, show: (v) => v ?? 'no LinkedIn id' },
  {
    read: (c) => (c.created_at === undefined ? undefined : formatDate(c.created_at)),
    show: (v) => (v === null ? 'added on an unknown date' : `added ${v}`),
  },
]

function known(value: string | null | undefined): string | null | undefined {
  if (value === undefined) return undefined
  const trimmed = value?.trim() ?? ''
  return trimmed === '' ? null : trimmed
}

/**
 * The two contacts of a merge, named so they never read alike. Different names
 * need nothing more. One name gets the first detail that differs between the
 * two, of the primary email, the company, the LinkedIn slug, and the date each
 * was added, and else the contact id: "Ada Quill (ada@quill.test)" against
 * "Ada Quill (ada.q@quill.test)". A detail unknown on either side is skipped.
 * Every place the merge names the two uses this, so each reads the same throughout.
 */
export function labelPair(one: Nameable, other: Nameable): [string, string] {
  const names = [displayName(one), displayName(other)] as const
  if (names[0].toLowerCase() !== names[1].toLowerCase()) return [names[0], names[1]]
  for (const detail of DETAILS) {
    const a = known(detail.read(one))
    const b = known(detail.read(other))
    if (a === undefined || b === undefined || a === b) continue
    return [`${names[0]} (${detail.show(a)})`, `${names[1]} (${detail.show(b)})`]
  }
  return [`${names[0]} (contact ${one.id})`, `${names[1]} (contact ${other.id})`]
}

function count(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

/** "1 link moves", "2 links move". */
function moving(n: number, one: string, many?: string): string {
  return `${count(n, one, many)} ${n === 1 ? 'moves' : 'move'}`
}

/** The moves in sentences; one line per kind that has any, or one saying nothing moves. */
export function describeMoves(moves: MergeMoves): string[] {
  const lines: string[] = []
  const child = (moved: { moved: number; dropped: number }, one: string, many?: string) => {
    if (moved.moved === 0 && moved.dropped === 0) return
    let line = moving(moved.moved, one, many)
    if (moved.dropped > 0) line += `; ${moved.dropped} already on the survivor`
    lines.push(line)
  }
  child(moves.emails, 'email address', 'email addresses')
  child(moves.phones, 'phone number')
  child(moves.links, 'link')
  child(moves.positions, 'position')
  if (moves.interactions > 0) lines.push(moving(moves.interactions, 'interaction'))
  if (moves.snapshots > 0) lines.push(moving(moves.snapshots, 'snapshot'))
  if (moves.tags_added.length > 0) lines.push(`Tags added: ${moves.tags_added.join(', ')}`)
  if (moves.tags_removed.length > 0) {
    lines.push(
      `Tags removed, as the other contact suppressed them: ${moves.tags_removed.join(', ')}`,
    )
  }
  if (moves.lists_added.length > 0) lines.push(`Lists joined: ${moves.lists_added.join(', ')}`)
  if (moves.enrollments_moved > 0) {
    lines.push(moving(moves.enrollments_moved, 'campaign enrollment'))
  }
  if (moves.enrollments_combined > 0) {
    lines.push(
      `${count(moves.enrollments_combined, 'campaign enrollment')} ${moves.enrollments_combined === 1 ? 'combines' : 'combine'} with the survivor’s in the same campaign`,
    )
  }
  if (moves.messages_moved > 0) lines.push(moving(moves.messages_moved, 'campaign message'))
  if (moves.messages_discarded > 0) {
    lines.push(
      `${count(moves.messages_discarded, 'unsent campaign message')} ${moves.messages_discarded === 1 ? 'is' : 'are'} discarded, so no step goes out twice`,
    )
  }
  if (moves.history_rows > 0) {
    lines.push(moving(moves.history_rows, 'old-campaign record'))
  }
  if (lines.length === 0)
    lines.push('Nothing but the fields above: the other contact has no other records.')
  return lines
}
