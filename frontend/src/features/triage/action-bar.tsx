/**
 * Every key in the map, as a button that shows its key.
 *
 * The screen stays keyboard-first — that is the throughput budget — but nothing
 * on it is keyboard-*only*. The row is generated from `BAR_BINDINGS`, so an
 * action cannot be added to the keyboard without appearing on screen, and each
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
 * **Met, Not met and Skip are not here (#142).** They are drawn once, in
 * `CardSteps`, under the question they answer. This row sits *above* the card,
 * so a second copy of them here would put the met call above the name and the
 * tags a person is meant to fix first — which is the ordering CP2.5's feedback
 * asked us to undo — and two rows of the same three buttons are two answers to
 * "where do I decide?".
 *
 * Nothing is lost when there is no card: there is then nothing to decide about.
 * `EmptyQueue` offers what does make sense there (revisit the skipped, go back,
 * undo), and a stray `m` still answers for itself through `onAction`'s
 * `nothing()` path — "No contact is on screen yet, so m did nothing" — which is
 * the same explanation a disabled button could never give.
 */

import { Button } from '@/components/ui/button'

import { BAR_BINDINGS, type TriageAction } from './keymap'

/**
 * How loud each button is. Name and Tag lead; the rest is movement and undo.
 *
 * `Partial`, because the map is keyed by every action and this row no longer
 * draws every action: an action with no entry here is not on this row at all,
 * and `BAR_BINDINGS` is what decides that.
 */
const VARIANTS: Partial<Record<TriageAction, 'default' | 'outline' | 'ghost'>> = {
  'preferred-name': 'default',
  tag: 'default',
  back: 'outline',
  next: 'outline',
  undo: 'ghost',
  help: 'ghost',
}

/** A hairline before these, so edits, movement, and undo read as groups. */
const BREAK_BEFORE: ReadonlySet<TriageAction> = new Set<TriageAction>(['back', 'undo'])

export function ActionBar({ onAction }: { onAction: (action: TriageAction) => void }) {
  return (
    <div
      role="group"
      aria-label="Triage actions"
      data-testid="triage-actions"
      className="flex flex-wrap items-center gap-1.5 rounded-xl bg-card p-2 ring-1 ring-foreground/10"
    >
      {BAR_BINDINGS.map((binding) => (
        <div key={binding.action} className="contents">
          {BREAK_BEFORE.has(binding.action) && (
            <span aria-hidden="true" className="mx-1 h-5 w-px bg-foreground/10" />
          )}
          <Button
            size="sm"
            variant={VARIANTS[binding.action] ?? 'ghost'}
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
