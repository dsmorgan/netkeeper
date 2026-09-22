/**
 * What a chosen file is, by its name alone (spec 10.5, P1-21).
 *
 * `Connections.csv` on its own is deliberately not one of these: it still
 * goes through the mapping screen, because that is the one place a row that
 * needs a closer look (a connection the archive importer only counts as
 * `needs_review`) can actually be resolved — the archive endpoint has no
 * candidate-review step to do that with. `messages.csv` and `Invitations.csv`
 * have no such path either way: the mapping screen has no field to point
 * their columns at, so the archive endpoint is the only way to import either
 * on its own.
 */
export type ArchiveKind = 'archive' | 'messages' | 'invitations'

/** `null` when `name` is not one this flow recognizes on sight. */
export function archiveKindOf(name: string): ArchiveKind | null {
  const trimmed = name.trim().toLowerCase()
  if (trimmed.endsWith('.zip')) return 'archive'
  if (trimmed === 'messages.csv') return 'messages'
  if (trimmed === 'invitations.csv') return 'invitations'
  return null
}
