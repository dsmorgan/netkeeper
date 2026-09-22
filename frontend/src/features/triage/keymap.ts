/**
 * The keyboard map of spec 10.2, in one place.
 *
 * The handler, the help overlay (`?`), and the button row all read this array,
 * so the screen cannot drift from the specification: a key that is not here is
 * not handled, a key that is here is documented, and every action that can be
 * taken with a key can be taken with the mouse.
 *
 *   `m` met · `n` not met · `s` skip · `u` undo · `t` tag ·
 *   `p` edit preferred name · `→` next · `←` back · `?` the map itself
 *
 * `←` is the *navigation* half of the pair the screen used to collapse into `u`
 * alone (P1-23): `←` moves, `u` writes. It and `?` were not in the first
 * writing of spec 10.2; §10.2 lists them now, so this array and the spec agree
 * and there is no "which of these are really the spec's" flag to keep in step.
 *
 * **`button` is the rule that the keyboard is a shortcut, not the only way in.**
 * Every binding carries the label of its on-screen button, so a person learns
 * the map by using the screen rather than by reading it. Escape is the one
 * exception, and it is not an exception to the rule: it dismisses whatever is
 * open, and every overlay carries its own Close, which *is* that button. A
 * global Escape button with nothing open would do nothing at all.
 */

export type TriageAction =
  | 'met'
  | 'not-met'
  | 'skip'
  | 'back'
  | 'next'
  | 'tag'
  | 'preferred-name'
  | 'undo'
  | 'help'
  | 'dismiss'

export interface KeyBinding {
  /** What `KeyboardEvent.key` carries, lowercased for letters. */
  readonly key: string
  /** How the key is printed to the person. */
  readonly label: string
  /**
   * The same key as `aria-keyshortcuts` spells it, which is the DOM's own
   * `KeyboardEvent.key` casing rather than the one we match on.
   */
  readonly aria: string
  readonly action: TriageAction
  readonly description: string
  /** The on-screen button's label, or `null` when the overlay's Close is it. */
  readonly button: string | null
}

export const KEY_BINDINGS: readonly KeyBinding[] = [
  {
    key: 'm',
    label: 'm',
    aria: 'm',
    action: 'met',
    description: 'Met this person',
    button: 'Met',
  },
  {
    key: 'n',
    label: 'n',
    aria: 'n',
    action: 'not-met',
    description: 'Have not met them',
    button: 'Not met',
  },
  {
    key: 's',
    label: 's',
    aria: 's',
    action: 'skip',
    description: 'Skip — revisit later with the Skipped filter',
    button: 'Skip',
  },
  {
    key: 'arrowleft',
    label: '←',
    aria: 'ArrowLeft',
    action: 'back',
    description: 'Back to a contact you already passed — writes nothing',
    button: 'Back',
  },
  {
    key: 'arrowright',
    label: '→',
    aria: 'ArrowRight',
    action: 'next',
    description: 'Next contact without deciding, and forward again while you are looking back',
    button: 'Next',
  },
  {
    key: 't',
    label: 't',
    aria: 't',
    action: 'tag',
    description: 'Tag this contact',
    button: 'Tag',
  },
  {
    key: 'p',
    label: 'p',
    aria: 'p',
    action: 'preferred-name',
    description: 'Edit the preferred name',
    button: 'Name',
  },
  {
    key: 'u',
    label: 'u',
    aria: 'u',
    action: 'undo',
    description: 'Undo the last thing written — not the same as going back',
    button: 'Undo',
  },
  {
    key: '?',
    label: '?',
    aria: '?',
    action: 'help',
    description: 'Show or hide this help',
    button: 'Keyboard',
  },
  {
    key: 'escape',
    label: 'Esc',
    aria: 'Escape',
    action: 'dismiss',
    description: 'Close the help, an editor, or a prompt',
    button: null,
  },
]

const BY_KEY = new Map(KEY_BINDINGS.map((binding) => [binding.key, binding]))
const BY_ACTION = new Map(KEY_BINDINGS.map((binding) => [binding.action, binding]))

/** The action a `KeyboardEvent.key` means, or `null` when the screen ignores it. */
export function actionFor(key: string): TriageAction | null {
  return BY_KEY.get(key.toLowerCase())?.action ?? null
}

/** The binding for an action, for naming the key that was just pressed. */
export function bindingFor(action: TriageAction): KeyBinding | undefined {
  return BY_ACTION.get(action)
}

/** Every action with an on-screen button, in the order the row draws them. */
export const BUTTON_BINDINGS = KEY_BINDINGS.filter(
  (binding): binding is KeyBinding & { button: string } => binding.button !== null,
)
