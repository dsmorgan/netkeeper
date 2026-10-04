/**
 * A merge's moves in sentences (#363), for the preview and its confirmation.
 */

import type { MergeMoves } from './types'

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
