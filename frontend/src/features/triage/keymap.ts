/**
 * The keyboard map of spec 10.2, in one place.
 *
 * The handler, the help overlay (`?`), and the hints under the card all read
 * this array, so the screen cannot drift from the specification: a key that is
 * not here is not handled, and a key that is here is documented.
 *
 *   `m` met · `n` not met · `s` skip · `u` undo · `t` tag ·
 *   `p` edit preferred name · `→` next
 *
 * `?` is not in the specification's list; it is the help overlay the screen owes
 * a keyboard-first design, and Escape closes whatever is open.
 */

export type TriageAction =
  'met' | 'not-met' | 'skip' | 'undo' | 'tag' | 'preferred-name' | 'next' | 'help' | 'dismiss'

export interface KeyBinding {
  /** What `KeyboardEvent.key` carries, lowercased for letters. */
  readonly key: string
  /** How the key is printed to the person. */
  readonly label: string
  readonly action: TriageAction
  readonly description: string
  /** The seven keys of spec 10.2; `?` and Escape are this screen's own. */
  readonly spec: boolean
}

export const KEY_BINDINGS: readonly KeyBinding[] = [
  { key: 'm', label: 'm', action: 'met', description: 'Met this person', spec: true },
  { key: 'n', label: 'n', action: 'not-met', description: 'Have not met them', spec: true },
  {
    key: 's',
    label: 's',
    action: 'skip',
    description: 'Skip — revisit later with the Skipped filter',
    spec: true,
  },
  { key: 'u', label: 'u', action: 'undo', description: 'Undo the last action', spec: true },
  { key: 't', label: 't', action: 'tag', description: 'Tag this contact', spec: true },
  {
    key: 'p',
    label: 'p',
    action: 'preferred-name',
    description: 'Edit the preferred name',
    spec: true,
  },
  {
    key: 'arrowright',
    label: '→',
    action: 'next',
    description: 'Next contact without deciding',
    spec: true,
  },
  { key: '?', label: '?', action: 'help', description: 'Show or hide this help', spec: false },
  {
    key: 'escape',
    label: 'Esc',
    action: 'dismiss',
    description: 'Close the help, an editor, or a prompt',
    spec: false,
  },
]

const BY_KEY = new Map(KEY_BINDINGS.map((binding) => [binding.key, binding]))

/** The action a `KeyboardEvent.key` means, or `null` when the screen ignores it. */
export function actionFor(key: string): TriageAction | null {
  return BY_KEY.get(key.toLowerCase())?.action ?? null
}

/** The seven keys the specification names, for the hint row under the card. */
export const SPEC_BINDINGS = KEY_BINDINGS.filter((binding) => binding.spec)
