/**
 * What each export preset holds, and what it quietly does not.
 *
 * Its own module because both the dialog and the Exports page read it, and a
 * file that exports a table beside a component loses fast refresh.
 */
import type { ExportPreset } from './types'

interface PresetSpec {
  value: ExportPreset
  label: string
  description: string
  caveat?: string
  /**
   * The file can hold fewer rows than the filter counts: the backend skips
   * contacts that would not re-import (`_REIMPORTABLE_PRESETS`) or that are
   * marked do-not-contact. The dialog's headline count says so.
   */
  dropsRows: boolean
}

export const EXPORT_PRESETS: readonly PresetSpec[] = [
  {
    value: 'nine-column',
    label: 'Nine-column',
    description:
      'The eight columns a mail-merge tool imports: profile URL, email, first and last name, city and state, company, title, phone.',
    caveat:
      'The file round-trips; a contact with two names does not. “First Name” carries the preferred name out and comes back as the first name, so someone stored as “Robert” who goes by “Bob” exports as “Bob” and reimports with both names set to “Bob”. Contacts with nothing identifying in these columns, and phone numbers with no digits, are left out rather than written as rows that would not import.',
    dropsRows: true,
  },
  {
    value: 'linkedin-archive',
    label: 'LinkedIn archive',
    description:
      'The columns of LinkedIn’s own Connections.csv, so a re-import sees values shaped the way LinkedIn reports them.',
    caveat:
      'These are the LinkedIn-sourced first and last names, not the triaged preferred name. The three-line preamble LinkedIn writes above its header is not reproduced. Contacts with nothing identifying in these columns are left out rather than written as rows that would not import.',
    dropsRows: true,
  },
  {
    value: 'full',
    label: 'Everything',
    description:
      'Every user-facing field, emails, phones, links, positions, and tags. Database ids and sync bookkeeping stay behind.',
    dropsRows: false,
  },
  {
    value: 'campaign-audience',
    label: 'Campaign audience',
    description: 'The campaign merge fields plus the recipient email, ready for a mail merge.',
    caveat:
      'Everyone marked do-not-contact is left out, because a mail-merge file is a send path once it leaves this tool. Expect fewer rows than the count above — that gap is the point, not a miscount.',
    dropsRows: true,
  },
]
