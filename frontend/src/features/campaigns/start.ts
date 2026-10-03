/** A scheduled start being chosen (#338): see `start-picker.tsx`. */
import { fromLocalInput } from './format'

/** `now`, or a `datetime-local` value in the reader's zone ('' until the default loads). */
export interface StartChoice {
  now: boolean
  value: string
}

export const UNCHOSEN: StartChoice = { now: false, value: '' }

/** The start to send, as an ISO time with its zone; null when the value is not a time. */
export function startIso(choice: StartChoice): string | null {
  return choice.now ? new Date().toISOString() : fromLocalInput(choice.value)
}
