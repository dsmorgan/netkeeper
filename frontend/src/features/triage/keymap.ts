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
 * **The array's order is the order the screen draws them, and it is not the
 * order they are listed in the spec.** `p` and `t` come first because that is
 * the order the work is done in: you fix the name and put the tags on while you
 * are looking at the person, and the met call is the last thing you do on a
 * card (#142). The keys themselves did not move — `m`, `n`, `s`, `t` and `p`
 * still fire from anywhere on the screen, so a run by somebody who knows the
 * map costs exactly what it cost before and only the reading order changed.
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
 *
 * **`where` says which row draws that button, not whether one exists.** The
 * three decisions are drawn on the card and nowhere else (#142); everything
 * else is in the action row. The `?` overlay reads this array whole, so it
 * still teaches all nine keys whichever row their button ended up in.
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
  /**
   * Where that button is drawn.
   *
   * `bar` is the action row; `card` is the decision row under "have you met
   * them?", which is the only place the three answers are offered (#142);
   * `overlay` is Escape, whose button is whatever is open.
   *
   * Every binding still lives in this one array whatever its `where`, so the
   * `?` overlay documents all of them and no key can be handled without being
   * written down.
   */
  readonly where: 'bar' | 'card' | 'overlay'
}

export const KEY_BINDINGS: readonly KeyBinding[] = [
  {
    key: 'p',
    label: 'p',
    aria: 'p',
    action: 'preferred-name',
    description: 'Edit the preferred name',
    button: 'Name',
    where: 'bar',
  },
  {
    key: 't',
    label: 't',
    aria: 't',
    action: 'tag',
    description: 'Tag this contact',
    button: 'Tag',
    where: 'bar',
  },
  {
    key: 'm',
    label: 'm',
    aria: 'm',
    action: 'met',
    description: 'Met this person',
    button: 'Met',
    where: 'card',
  },
  {
    key: 'n',
    label: 'n',
    aria: 'n',
    action: 'not-met',
    description: 'Have not met them',
    button: 'Not met',
    where: 'card',
  },
  {
    key: 's',
    label: 's',
    aria: 's',
    action: 'skip',
    description: 'Skip — revisit later with the Skipped filter',
    button: 'Skip',
    where: 'card',
  },
  {
    key: 'arrowleft',
    label: '←',
    aria: 'ArrowLeft',
    action: 'back',
    description: 'Back to a contact you already passed — writes nothing',
    button: 'Back',
    where: 'bar',
  },
  {
    key: 'arrowright',
    label: '→',
    aria: 'ArrowRight',
    action: 'next',
    description: 'Next contact without deciding, and forward again while you are looking back',
    button: 'Next',
    where: 'bar',
  },
  {
    key: 'u',
    label: 'u',
    aria: 'u',
    action: 'undo',
    description: 'Undo the last thing written — not the same as going back',
    button: 'Undo',
    where: 'bar',
  },
  {
    key: '?',
    label: '?',
    aria: '?',
    action: 'help',
    description: 'Show or hide this help',
    button: 'Keyboard',
    where: 'bar',
  },
  {
    key: 'escape',
    label: 'Esc',
    aria: 'Escape',
    action: 'dismiss',
    description: 'Close the help, an editor, or a prompt',
    button: null,
    where: 'overlay',
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

/**
 * Every action with an on-screen button, wherever that button is drawn.
 *
 * The invariant `triage-controls.test.tsx` holds: an action cannot gain a key
 * without gaining a button somebody can press. Which *row* it is on is
 * `where`'s business, and the two subsets below are what each row draws.
 */
export const BUTTON_BINDINGS = KEY_BINDINGS.filter(
  (binding): binding is KeyBinding & { button: string } => binding.button !== null,
)

/**
 * What the action row draws, in order: the edits, then movement, then undo.
 *
 * **The three decisions are deliberately not here (#142).** The row sits above
 * the card, so a copy of Met/Not met/Skip in it would put the met call above
 * the name and the tags a person is meant to fix first, which is the ordering
 * this change exists to undo — and two rows of the same three buttons are two
 * answers to "where do I decide?". They are drawn once, on the card, under the
 * question they answer. The keys are unaffected: `m`, `n` and `s` fire from
 * anywhere, and with no card on screen the handler says so rather than
 * silently dropping the press.
 */
export const BAR_BINDINGS = BUTTON_BINDINGS.filter((binding) => binding.where === 'bar')

/** What the card's decision row draws: the three answers, in the order asked. */
export const DECISION_BINDINGS = BUTTON_BINDINGS.filter((binding) => binding.where === 'card')
