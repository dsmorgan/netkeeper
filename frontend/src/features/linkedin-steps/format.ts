/**
 * Words for the LinkedIn step screens (#383): why a prefill was refused, and how
 * long a prefilled message has waited.
 */
import { WHY_IT_MATTERS, type LintRule } from '@/features/templates/lint'

import type { WaitingItem } from './api'

export const ONE_AT_A_TIME = 'One prefill at a time: send or discard the open one first.'
export const TYPING = 'Typing in Chrome… watch the netkeeper Chrome window.'
export const REVIEW_AND_SEND = 'Review it in Chrome and click Send yourself.'

/** A prefilled message this old is stale (spec 11.6): it no longer holds the one slot. */
export const STALE_AFTER_DAYS = 3

/**
 * Each refusal reason the claim can give, in plain words. The claim's own
 * (`Refusal`), the engine's skips, and the guards' reasons
 * (`netkeeper/services/linkedin_steps.py`, `campaign_engine.py`, `campaign_guards.py`).
 * A reason not here is shown as the backend sent it.
 */
export const REASON_TEXT: Readonly<Record<string, string>> = {
  run_refused: "netkeeper can't run a prefill yet, so nothing was typed in Chrome",
  run_in_progress: 'another LinkedIn run is going; wait for it to finish',
  prefill_open: 'another prefill is open: send or discard it first',
  outside_active_hours: "it's outside your LinkedIn active hours",
  bad_active_hours: "your LinkedIn active hours can't be read; fix them in the config",
  not_a_linkedin_step: "this enrollment's next step isn't a LinkedIn step",
  no_step: 'this enrollment has no next step',
  rendered_errors: "the message for this contact has errors, so it's parked for you to fix",
  browser_unknown: 'nothing says the LinkedIn session is logged in; run a preflight first',
  browser_unhealthy: 'the LinkedIn session is flagged or heat is too high',
  browser_out_of_budget: "today's LinkedIn prefill budget is spent",
  replied: 'the contact replied, so the enrollment ended',
  ended: 'the enrollment ended',
  step_already_sent: 'this step already has a message',
  waiting_on_unsent: "an earlier message on this enrollment hasn't gone out yet",
  not_due: "this step isn't due yet",
  not_started: "the campaign hasn't started yet",
  outside_sending_hours: "it's outside your sending hours",
  spilled_to_next_day: 'your sending hours hold it until the next day',
  bad_schedule: "your sending hours can't be read; fix them in Settings",
  spacing: "the step's delay since the last step hasn't passed",
  template_errors: 'the template has lint errors',
  render_failed: "the message didn't render for this contact",
  guard_excluded: 'the guards leave this contact out',
  campaign_not_active: "the campaign isn't active",
  enrollment_not_active: "the enrollment isn't active",
  no_linkedin: 'the contact has no LinkedIn identity',
  do_not_contact: 'the contact is marked do not contact',
  archived: 'the contact is archived',
  merged: 'the contact was merged into another',
  self: "that's you",
  needs_review: 'the contact needs review first',
  contacted_recently: 'you contacted them recently',
  in_another_campaign: "they're in another active campaign",
}

/** One reason as words: the table above, a lint rule's own reason, or the code itself. */
export function reasonText(reason: string): string {
  const text = REASON_TEXT[reason]
  if (text !== undefined) return text
  if (reason in WHY_IT_MATTERS) {
    return `${reason.replace(/_/g, ' ')}: ${WHY_IT_MATTERS[reason as LintRule]}`
  }
  return reason.replace(/_/g, ' ')
}

/** Whole days between `iso` and `now`, never below zero. */
export function daysSince(iso: string, now: Date): number {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return 0
  return Math.max(0, Math.floor((now.getTime() - then) / 86_400_000))
}

/** "prefilled 2 hours ago", "prefilled 3 days ago"; "prefilled" when the time is unknown. */
export function ageText(iso: string | null, now: Date): string {
  if (iso === null) return 'not recorded'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const minutes = Math.max(0, Math.round((now.getTime() - then) / 60_000))
  const format = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  if (minutes < 60) return format.format(-minutes, 'minute')
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return format.format(-hours, 'hour')
  return format.format(-Math.floor(hours / 24), 'day')
}

/** What a waiting message needs from you: send it, a stale one, or a stopped prefill. */
export type WaitingState = 'prefilled' | 'stale' | 'interrupted'

export function waitingState(item: WaitingItem, now: Date): WaitingState {
  if (item.interrupted) return 'interrupted'
  if (item.status === 'stale') return 'stale'
  // The tick marks it `stale` within a minute; until then the age says it already is.
  if (item.prefilled_at !== null && daysSince(item.prefilled_at, now) >= STALE_AFTER_DAYS) {
    return 'stale'
  }
  return 'prefilled'
}

/**
 * The open prefill, if any: one typed and not stale yet, or one whose prefill stopped
 * before it said what it typed. Either holds the one slot (spec 11.6).
 */
export function openPrefill(items: readonly WaitingItem[], now: Date): WaitingItem | null {
  return items.find((item) => waitingState(item, now) !== 'stale') ?? null
}

/**
 * `https://www.linkedin.com/messaging/thread/<id>/` from a stored conversation URN:
 * `urn:li:msg_conversation:<id>`, or `urn:li:msg_conversation:(<profile urn>,<id>)`.
 * Null for anything else, so a malformed value never becomes a link.
 */
export function threadUrl(urn: string | null): string | null {
  if (urn === null) return null
  const match = /^urn:li:[a-z_]*conversation:(?:\([^,()]+,([^,()]+)\)|([^,():]+))$/i.exec(urn)
  const id = match?.[1] ?? match?.[2]
  if (id === undefined || id === '') return null
  return `https://www.linkedin.com/messaging/thread/${encodeURIComponent(id)}/`
}
