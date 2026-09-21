import { MET_LABELS } from './types'
import type { ContactsSearch } from './search'

/**
 * The filter in words, for the table header and the save-a-view dialog.
 *
 * The server sends its own reading of a selection back with a bulk count
 * (`BulkCountOut.describe`); that one is authoritative and is what a
 * confirmation shows. This one describes what is on screen before anything is
 * counted.
 */
export function describeSearch(search: ContactsSearch): string {
  const parts: string[] = []
  if (search.q) parts.push(`matching “${search.q}”`)
  if (search.company) parts.push(`at a company like “${search.company}”`)
  if (search.met) parts.push(MET_LABELS[search.met].toLowerCase())
  if (search.dnc) parts.push('marked do not contact')
  if (search.tags?.length) parts.push(`tagged ${search.tags.join(' or ')}`)
  const scope = search.archived ? 'Contacts, archived included' : 'Contacts'
  return parts.length === 0 ? `${scope}, unfiltered` : `${scope} ${parts.join(', ')}`
}
