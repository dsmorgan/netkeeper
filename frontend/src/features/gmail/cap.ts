import type { MailboxSends } from './api'

/** Within this share of the cap, the card warns before the cap is reached. */
export const NEAR_CAP = 0.8

/** The sentence a mailbox's count earns: none until it is near its cap. */
export function capWarning(sends: MailboxSends): string | null {
  if (sends.sent_today >= sends.daily_cap) {
    return `${sends.email} is at today’s cap. No more email goes out from it until tomorrow.`
  }
  if (sends.daily_cap > 0 && sends.sent_today >= sends.daily_cap * NEAR_CAP) {
    return `${sends.email} is close to today’s cap: ${sends.daily_cap - sends.sent_today} left.`
  }
  return null
}
