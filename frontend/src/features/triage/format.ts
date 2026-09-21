/** Dates and counts as the evidence panel prints them. Local time, short forms. */

const DAY = new Intl.DateTimeFormat(undefined, {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
})

const MINUTE = new Intl.DateTimeFormat(undefined, {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})

/** A `date` or `date-time` string as a day, or a dash when there is none. */
export function formatDay(value: string | null | undefined): string {
  if (!value) return '—'
  // A bare `YYYY-MM-DD` parses as UTC midnight, which prints as the day before
  // in western time zones; read it as a local day instead.
  const date = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T00:00:00`) : new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : DAY.format(date)
}

/** A timestamp down to the minute, for message rows. */
export function formatMinute(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : MINUTE.format(date)
}

/** `1 thread` / `4 threads`, without a library. */
export function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`
}
