/** Helpers for the sending hours (#338), shared by the Settings form and the campaign page. */
import type { Day, SendingHours, SendingHoursIn } from './api'

export const DAYS: readonly Day[] = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/** What is wrong with a draft, said before saving; null when it can be saved. */
export function sendingHoursProblem(draft: SendingHoursIn): string | null {
  if (!draft.enabled) return null
  if (draft.days.length === 0) return 'Choose at least one day.'
  if (draft.start === '' || draft.end === '') return 'Choose a start and an end time.'
  if (draft.end <= draft.start) return 'The end must be after the start.'
  return null
}

/** A warning for a step's time of day outside the sending hours; null inside or with none. */
export function stepTimeWarning(hours: SendingHours | undefined, time: string): string | null {
  if (hours === undefined || !hours.enabled || time === '') return null
  if (hours.start <= time && time < hours.end) return null
  return `${time} is outside the sending hours (${hours.summary}): the step waits for their next opening.`
}
