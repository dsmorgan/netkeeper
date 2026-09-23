/**
 * Every key in the map, as a button that shows its key.
 *
 * The screen stays keyboard-first — that is the throughput budget — but nothing
 * on it is keyboard-*only*. The row is generated from `KEY_BINDINGS`, so an
 * action cannot be added to the keyboard without appearing here, and each
 * button carries its key twice over: printed in a `kbd` for the eye, and in
 * `aria-keyshortcuts` for a screen reader. Using the screen is how the map gets
 * learned.
 *
 * The buttons go through the same `onAction` the keyboard does, synchronously,
 * so a click costs exactly what a keystroke costs and neither waits on the
 * network. Nothing here is disabled: an action with nothing to act on answers
 * with a line saying so, which is more use than a button that cannot be pressed
 * and does not say why.
 *
 * This row is on screen whatever the queue is doing — an empty queue, a failed
 * load, a contact still in flight — which is why the decisions are here as well
 * as on the card. `ContactCard`'s copy of them is the one to press while a
 * person is on screen: it asks about that person by name, and it carries the
 * definition of each answer. Both go through `onAction`, so neither is a second
 * path to the API.
 */

import { Button } from '@/components/ui/button'

import { BUTTON_BINDINGS, type TriageAction } from './keymap'

/**
 * How loud each button is, which follows the order the work is done in (#142).
 *
 * Name and Tag lead the row in solid styling: they are what you do to a card
 * *before* you decide anything, and a row that opened with the met call taught
 * the opposite. The decisions sit to their right in a quieter style because the
 * loud copy of them is on the card itself, under the question, where the
 * decision is actually made; this row is the key map, and its job is to teach
 * the keys rather than to compete with the card for the same press.
 */
const VARIANTS: Record<TriageAction, 'default' | 'secondary' | 'outline' | 'ghost'> = {
  'preferred-name': 'default',
  tag: 'default',
  met: 'secondary',
  'not-met': 'secondary',
  skip: 'outline',
  back: 'outline',
  next: 'outline',
  undo: 'ghost',
  help: 'ghost',
  dismiss: 'ghost',
}

/** A hairline before these, so edits, decisions, movement, and undo read as groups. */
const BREAK_BEFORE: ReadonlySet<TriageAction> = new Set<TriageAction>(['met', 'back', 'undo'])

export function ActionBar({ onAction }: { onAction: (action: TriageAction) => void }) {
  return (
    <div
      role="group"
      aria-label="Triage actions"
      data-testid="triage-actions"
      className="flex flex-wrap items-center gap-1.5 rounded-xl bg-card p-2 ring-1 ring-foreground/10"
    >
      {BUTTON_BINDINGS.map((binding) => (
        <div key={binding.action} className="contents">
          {BREAK_BEFORE.has(binding.action) && (
            <span aria-hidden="true" className="mx-1 h-5 w-px bg-foreground/10" />
          )}
          <Button
            size="sm"
            variant={VARIANTS[binding.action]}
            aria-keyshortcuts={binding.aria}
            onClick={() => onAction(binding.action)}
          >
            {binding.button}
            <kbd
              aria-hidden="true"
              className="rounded border border-current/25 px-1 font-mono text-[0.7rem] leading-4 opacity-70"
            >
              {binding.label}
            </kbd>
          </Button>
        </div>
      ))}
    </div>
  )
}
