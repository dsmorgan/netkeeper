import type { CheckState, MailboxPoll, PollCheck } from './api'

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** "just now", "3 min ago", "2 h ago", "4 days ago". */
export function ago(iso: string, now: number): string {
  const elapsed = Math.max(0, now - Date.parse(iso))
  if (elapsed < MINUTE) return 'just now'
  if (elapsed < HOUR) return `${Math.floor(elapsed / MINUTE)} min ago`
  if (elapsed < DAY) return `${Math.floor(elapsed / HOUR)} h ago`
  const days = Math.floor(elapsed / DAY)
  return days === 1 ? '1 day ago' : `${days} days ago`
}

/** Within the hour, "in 7 min"; later today, "14:20"; later than that, "Tue 14:20". */
export function nextText(iso: string, now: number): string {
  const at = Date.parse(iso)
  const ahead = at - now
  if (ahead < MINUTE) return 'in under a minute'
  if (ahead < HOUR) return `in ${Math.ceil(ahead / MINUTE)} min`
  const date = new Date(at)
  const time = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (date.toDateString() === new Date(now).toDateString()) return time
  return `${date.toLocaleDateString(undefined, { weekday: 'short' })} ${time}`
}

/**
 * "every minute", "every 10 min", "every 90 min", "every 3 h", "every day", "every 7 days".
 * An interval that is not a whole number of hours or days stays in the unit below.
 */
export function everyText(minutes: number): string {
  if (minutes <= 1) return 'every minute'
  if (minutes < 60 || minutes % 60 !== 0) return `every ${minutes} min`
  if (minutes < 1440 || minutes % 1440 !== 0) return `every ${minutes / 60} h`
  const days = minutes / 1440
  return days === 1 ? 'every day' : `every ${days} days`
}

/** What a check that has no next time says instead. Never a time. */
export const STATE_TEXT: Record<Exclude<CheckState, 'scheduled'>, string> = {
  due: 'due soon',
  idle: 'nothing to check',
  paused: 'paused',
  outside_hours: 'outside active hours',
  blocked: 'needs attention',
  off: 'off',
  not_running: 'not running',
  not_wired: 'not running yet',
}

/**
 * One check in a line: "checked 3 min ago · next in 7 min", "next 14:20",
 * "checked 2 h ago · paused". Only a `scheduled` check shows a next time.
 */
export function checkSummary(check: PollCheck, now: number): string {
  const parts: string[] = []
  if (check.last_at) parts.push(`checked ${ago(check.last_at, now)}`)
  if (check.state === 'scheduled' && check.next_at) {
    parts.push(`next ${nextText(check.next_at, now)}`)
  } else if (check.state !== 'scheduled') {
    parts.push(STATE_TEXT[check.state])
  }
  return parts.join(' · ')
}

/** A reason as a sentence: with its full stop. */
function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`
}

/** "Replies checked 3 min ago · next in 7 min", or why they are not checked. */
export function repliesText(poll: MailboxPoll, now: number): string {
  const last = poll.replies_polled_at
    ? `Replies checked ${ago(poll.replies_polled_at, now)}`
    : 'Replies not checked yet'
  if (poll.state === 'scheduled' && poll.next_at) {
    return `${last} · next ${nextText(poll.next_at, now)}`
  }
  if (poll.state === 'due') return `${last} · next check within a minute`
  return poll.reason ? `${last}. ${sentence(poll.reason)}` : `${last}.`
}
