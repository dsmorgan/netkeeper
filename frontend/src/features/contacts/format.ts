/** Shared cell formatting. Dates print as the API stores them; times print local. */

/** A `date` column arrives as `YYYY-MM-DD` and is shown as it is: no zone to shift it. */
export function formatDate(value: string | null | undefined): string | null {
  return value ? value.slice(0, 10) : null
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/** A timezone-aware timestamp, in the reader's own zone, to the minute. */
export function formatDateTime(value: string | null | undefined): string | null {
  if (!value) return null
  const at = new Date(value)
  if (Number.isNaN(at.getTime())) return null
  return (
    `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}` +
    ` ${pad(at.getHours())}:${pad(at.getMinutes())}`
  )
}

/** The name to call somebody by: their preferred name when they have one. */
export function displayName(contact: {
  preferred_name?: string | null
  first_name?: string | null
  last_name?: string | null
}): string {
  const first = contact.preferred_name?.trim() || contact.first_name?.trim() || ''
  const last = contact.last_name?.trim() ?? ''
  return [first, last].filter(Boolean).join(' ') || 'Unnamed contact'
}

/**
 * Gmail's search for an address. A link, never a request: the app itself never
 * talks to Gmail from the browser.
 */
export function gmailSearchUrl(email: string | null | undefined): string | null {
  return email ? `https://mail.google.com/mail/u/0/#search/${encodeURIComponent(email)}` : null
}
