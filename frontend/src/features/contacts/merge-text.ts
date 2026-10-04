/**
 * A merge's moves in sentences (#363), for the preview and its confirmation.
 */

import { displayName } from './format'
import type { ContactDetail, MergeMoves } from './types'

/** What `labelled` needs: a name, and whatever of the distinguishing details is known. */
export interface Nameable {
  id: number
  first_name?: string | null
  last_name?: string | null
  preferred_name?: string | null
  primary_email?: string | null
  current_company?: string | null
  li_public_id?: string | null
}

/**
 * A name with one detail that tells it apart, so two records of one name never
 * read the same in a merge: the primary email, else the company, else the
 * LinkedIn slug, else the contact id. "Ada Quill (ada@quill.test)".
 */
export function labelled(contact: Nameable): string {
  const detail =
    contact.primary_email ||
    contact.current_company ||
    contact.li_public_id ||
    `contact ${contact.id}`
  return `${displayName(contact)} (${detail})`
}

/** {@link labelled} for a contact in full, whose primary email is among its emails. */
export function labelledDetail(contact: ContactDetail): string {
  const email = contact.emails.find((row) => row.is_primary) ?? contact.emails[0]
  return labelled({ ...contact, primary_email: email?.email ?? null })
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
