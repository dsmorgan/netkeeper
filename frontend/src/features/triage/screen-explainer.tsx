/**
 * What this screen is for, said once, above the card (#142).
 *
 * The first real run of triage produced "I don't actually know what you want me
 * to decide here", which is a fair reading of a screen that opened on a
 * stranger's headline and three unlabelled verbs. The answer is in
 * `docs/networking-workflow.md` and it is short, so it is on the screen in the
 * method's own words (`method.ts`) rather than paraphrased here.
 *
 * **The goal line is always on screen; the definitions start folded.** An
 * earlier draft opened the whole thing by default and pushed the decision row
 * off the bottom of a 1280x800 laptop — which made the one control the screen
 * exists for the one thing a first-time user could not see. Nothing is lost by
 * folding it: the card carries a one-line definition of each answer next to the
 * button that gives it, in both states, so "what does Met mean?" is answerable
 * without opening anything and this is the longer version for somebody who
 * wants it. Once opened it stays open, because the person who opened it means
 * it. That preference is the one thing on this screen worth a `localStorage`
 * entry: per-viewer, a convenience, and losing it costs one click.
 *
 * **Every read and write of it is wrapped.** `localStorage` throws outright in
 * a private window with site data blocked, and comes back empty in a fresh
 * profile, in a test, and after somebody clears their browser. Neither may stop
 * the screen from rendering, and neither may change what it renders beyond
 * whether the definitions start folded out: an empty read means "never opened
 * it", which is the default anyway.
 *
 * Not a `<details>`: the toggle is a button with `aria-expanded`, so the label
 * says what pressing it does in both states, and nothing here takes a key the
 * map owns.
 */

import { useState } from 'react'

import { Button } from '@/components/ui/button'

import { DECISION_MEANINGS, TRIAGE_GOAL } from './method'

/** Per-viewer, per-browser. Nothing about it reaches the server or another tab. */
const STORAGE_KEY = 'netkeeper.triage.explainer'
const OPEN = 'open'

/** Whether this viewer has opened it before. `false` whenever the answer is unknown. */
function wasOpened(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === OPEN
  } catch {
    // A private window can throw on the read itself, not only on the write.
    return false
  }
}

function remember(open: boolean): void {
  try {
    if (open) window.localStorage.setItem(STORAGE_KEY, OPEN)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing to do and nothing to report: the section still works, it just
    // folds again next time.
  }
}

export function ScreenExplainer() {
  const [open, setOpen] = useState(wasOpened)

  function toggle() {
    const next = !open
    setOpen(next)
    remember(next)
  }

  return (
    <section
      aria-label="What this screen is for"
      data-testid="triage-explainer"
      className="flex flex-col gap-2 rounded-xl bg-muted/50 px-3 py-2 ring-1 ring-foreground/10"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 text-sm">
          <span className="font-medium">{TRIAGE_GOAL}</span>
        </p>
        <Button
          size="xs"
          variant="ghost"
          aria-expanded={open}
          aria-controls="triage-explainer-body"
          onClick={toggle}
        >
          {open ? 'Hide this' : 'What counts as met?'}
        </Button>
      </div>

      {open && (
        <dl
          id="triage-explainer-body"
          className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm"
        >
          {DECISION_MEANINGS.map((meaning) => (
            <div key={meaning.action} className="contents">
              <dt className="font-medium">{meaning.term}</dt>
              <dd className="min-w-0 text-muted-foreground">{meaning.long}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  )
}
