/**
 * What this screen is for, said once, above the card (#142).
 *
 * The first real run of triage produced "I don't actually know what you want me
 * to decide here", which is a fair reading of a screen that opened on a
 * stranger's headline and three unlabelled verbs. The answer is in
 * `docs/networking-workflow.md` and it is short, so it is on the screen in the
 * method's own words (`method.ts`) rather than paraphrased here.
 *
 * Open by default, because the person who needs it has not read it yet. Once
 * collapsed it stays collapsed, because the person who closed it has. That
 * preference is the one thing on this screen worth a `localStorage` entry: it
 * is per-viewer, it is a convenience, and losing it costs one click.
 *
 * **Every read and write of it is wrapped.** `localStorage` throws outright in
 * a private window with site data blocked, and comes back empty in a fresh
 * profile, in a test, and after somebody clears their browser. Neither may stop
 * the screen from rendering, and neither may change what it renders beyond
 * whether this section starts open: an empty read means "never collapsed", which
 * is the default anyway.
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
const COLLAPSED = 'collapsed'

/** Whether this viewer has closed it before. `false` whenever the answer is unknown. */
function wasCollapsed(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === COLLAPSED
  } catch {
    // A private window can throw on the read itself, not only on the write.
    return false
  }
}

function remember(collapsed: boolean): void {
  try {
    if (collapsed) window.localStorage.setItem(STORAGE_KEY, COLLAPSED)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing to do and nothing to report: the section still works, it just
    // opens again next time.
  }
}

export function ScreenExplainer() {
  const [collapsed, setCollapsed] = useState(wasCollapsed)

  function toggle() {
    const next = !collapsed
    setCollapsed(next)
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
          aria-expanded={!collapsed}
          aria-controls="triage-explainer-body"
          onClick={toggle}
        >
          {collapsed ? 'What counts as met?' : 'Hide this'}
        </Button>
      </div>

      {!collapsed && (
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
