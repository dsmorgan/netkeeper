/**
 * The global key handler for the triage screen.
 *
 * It listens on `window`, not on a focused element, because the screen must work
 * without anyone having tabbed anywhere: a person who lands on `/triage` presses
 * `m` and it counts. That means the handler has to stand aside deliberately
 * rather than by accident.
 *
 * It stands aside when:
 *
 * - a modifier is held, so browser and OS shortcuts keep working;
 * - the event is already handled (`defaultPrevented`), so a component that
 *   binds a key for itself wins;
 * - the target is a text field, a select, or a `contenteditable`, so typing a
 *   name into the `p` editor types it instead of marking people met;
 * - the keystroke is an auto-repeat.
 *
 * The text-field rule is the whole reason this screen has no modal: the editors
 * and the help overlay move focus but never trap it, and the map stays live
 * except where a keystroke obviously means a letter.
 *
 * The auto-repeat rule is about what a key *means* here. Every key on this
 * screen is a judgement about one person, and a judgement cannot be held down:
 * a finger resting on `m` for a second marks thirty people met, and the
 * two-card buffer does not slow it, because it rate-limits a burst inside one
 * tick and an auto-repeat arrives as thirty separate ones. `→` is excluded for
 * the same reason rather than a different one — holding it skims past contacts
 * too fast to have been read, and each repeat costs a request.
 */

import { useEffect } from 'react'

import { actionFor, type TriageAction } from './keymap'

const EDITABLE = new Set(['INPUT', 'TEXTAREA', 'SELECT'])

/** True when the keystroke belongs to whatever the person is typing into. */
function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return EDITABLE.has(target.tagName) || target.isContentEditable
}

export function useTriageKeys(options: {
  enabled: boolean
  onAction: (action: TriageAction) => void
}): void {
  const { enabled, onAction } = options

  useEffect(() => {
    if (!enabled) return
    function handle(event: KeyboardEvent) {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return
      if (event.repeat) return
      if (isTyping(event.target)) return
      const action = actionFor(event.key)
      if (action === null) return
      event.preventDefault()
      onAction(action)
    }
    window.addEventListener('keydown', handle)
    return () => window.removeEventListener('keydown', handle)
  }, [enabled, onAction])
}
