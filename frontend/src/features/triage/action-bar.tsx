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
 */

import { Button } from '@/components/ui/button'

import { BUTTON_BINDINGS, type TriageAction } from './keymap'

/** How loud each button is. The three decisions are the work; the rest is around it. */
const VARIANTS: Record<TriageAction, 'default' | 'outline' | 'ghost'> = {
  met: 'default',
  'not-met': 'default',
  skip: 'outline',
  back: 'outline',
  next: 'outline',
  tag: 'ghost',
  'preferred-name': 'ghost',
  undo: 'ghost',
  help: 'ghost',
  dismiss: 'ghost',
}

/** A hairline before these, so decisions, movement, and edits read as groups. */
const BREAK_BEFORE: ReadonlySet<TriageAction> = new Set<TriageAction>(['back', 'tag', 'undo'])

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
