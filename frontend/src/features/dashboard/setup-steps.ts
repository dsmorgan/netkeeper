/**
 * The setup path: what each step is, its real count, and whether it is done,
 * in progress, or not started.
 *
 * A pure function so the state logic is tested without rendering anything,
 * and so a later phase adds its own step (connect LinkedIn in phase 2,
 * connect Gmail in phase 3) by appending one more entry to the array this
 * builds, not by reshaping the page around it.
 *
 * `StepState` has three values, and all three describe the *user's*
 * progress. A step whose completion nothing can verify does not get a fourth
 * state wedged into that ladder — its `state` is `null` (no badge), and its
 * `detail` says the true thing in words instead. Two steps land there once
 * there are contacts to act on:
 *
 * - **Export** has no signal at all: exports are a browser download, not a
 *   tracked action, and nothing in the API records that one ran.
 * - **Build a list** has a signal that lies: `GET /lists` always includes the
 *   built-in "Validated" smart list the backend seeds at every server start
 *   (`ensure_validated_list`, `netkeeper/web/app.py`), and nothing in the
 *   response marks it as built-in — a fresh install with zero user-made
 *   lists still returns one. Counting raw length would read "Done" the
 *   moment a server has ever started, for a person who has built nothing, so
 *   this step never claims `done` or `not_started` — only the honest count.
 *
 * "Review your tags" is not one of those two: `stats.tagged` is a real,
 * checkable count (a contact carrying any `ContactTag`), so `tagged === 0`
 * is a real `not_started`. What it is *not* is a count of automatic tags —
 * `ContactTag.source` is `manual | rule` and the stats endpoint does not
 * split on it, so a hand-applied tag counts the same as a rule's. Until the
 * stats endpoint adds a rule-sourced count, this step's title and detail
 * never say "automatically", and its `done`-shaped state (`tagged > 0`) is
 * `null` too — a real count, but not a claim about how it got there or
 * whether anyone reviewed it.
 */
import type { ContactStats, ImportRunPage } from './api'
import type { ListOut } from '@/features/crm/types'

export type StepState = 'not_started' | 'in_progress' | 'done'

export type StepRoute = '/imports' | '/triage' | '/lists' | '/exports'

export interface SetupStep {
  key: string
  title: string
  /** What the step says about itself right now — a real count wherever one exists. */
  detail: string
  /** `null` when nothing lets this step claim progress; see the module doc. */
  state: StepState | null
  to: StepRoute
  /** Only the import step ever resumes a specific run. */
  search?: { run: number }
  cta: string
}

export interface SetupStepInputs {
  /** `undefined` while pending or on error — the page gates on this before rendering steps. */
  stats: ContactStats | undefined
  /** `undefined` when the draft-imports query has not answered; drafts are then assumed unknown. */
  openImports?: ImportRunPage
  /** True when the draft-imports query itself failed, so the import step says so rather than guessing "done". */
  openImportsUnavailable?: boolean
  lists?: ListOut[]
  /** True when the lists query itself failed, so the list step says so rather than guessing a count. */
  listsUnavailable?: boolean
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

export function buildSetupSteps({
  stats,
  openImports,
  openImportsUnavailable = false,
  lists,
  listsUnavailable = false,
}: SetupStepInputs): SetupStep[] {
  const total = stats?.total ?? 0
  const hasContacts = total > 0

  // --- import ---------------------------------------------------------------

  const draftsKnown = !openImportsUnavailable
  const draftTotal = draftsKnown ? (openImports?.total ?? 0) : 0
  const newestDraftId = openImports?.items[0]?.id

  const importState: StepState | null = !draftsKnown
    ? null
    : draftTotal > 0
      ? 'in_progress'
      : hasContacts
        ? 'done'
        : 'not_started'
  const importDetail = !draftsKnown
    ? hasContacts
      ? `${plural(total, 'contact')} imported; open imports could not be checked`
      : 'Open imports could not be checked'
    : draftTotal > 0
      ? `${plural(draftTotal, 'draft import')} waiting to be finished` +
        (draftTotal > 1 ? ' — resuming the most recent' : '')
      : hasContacts
        ? `${plural(total, 'contact')} imported`
        : 'Nothing imported yet'
  const importCta = draftsKnown && draftTotal > 0 ? 'Continue this import' : 'Import contacts'

  // --- review tags ------------------------------------------------------------
  //
  // `tagged` is real; "automatically" is not (see module doc). `tagged === 0`
  // is the one claim about this count that is both true and actionable — it
  // is also true whether or not a later lane makes auto-tag rules run on
  // import, since it reads the live count rather than assuming import always
  // leaves it at zero.

  const tagged = stats?.tagged ?? 0
  const reviewNotStarted = !hasContacts || tagged === 0
  const reviewState: StepState | null = reviewNotStarted ? 'not_started' : null
  const reviewDetail = reviewNotStarted
    ? 'Nothing tagged yet'
    : `${plural(tagged, 'contact')} tagged`
  const reviewCta = hasContacts && tagged === 0 ? 'Run auto-tag rules' : 'Review tags'

  // --- triage -----------------------------------------------------------------

  const untriaged = stats?.untriaged ?? 0
  const triagedCount = Math.max(total - untriaged, 0)
  const triageState: StepState = !hasContacts
    ? 'not_started'
    : untriaged === 0
      ? 'done'
      : 'in_progress'
  const triageDetail = hasContacts ? `${triagedCount} of ${total} triaged` : 'Nothing to triage yet'

  // --- build a list -------------------------------------------------------------
  //
  // Never `done`, never `not_started`: see the module doc on the seeded
  // "Validated" list. The count is real; what it counts is not "lists you
  // built".

  const listsKnown = !listsUnavailable
  const listCount = lists?.length ?? 0
  const listDetail = listsKnown ? plural(listCount, 'list') : 'List count could not be checked'

  // --- export -------------------------------------------------------------------

  const exportState: StepState | null = hasContacts ? null : 'not_started'
  const exportDetail = hasContacts
    ? 'Not tracked — export runs whenever you like'
    : 'Nothing to export yet'

  return [
    {
      key: 'import',
      title: 'Import your data',
      detail: importDetail,
      state: importState,
      to: '/imports',
      search:
        draftsKnown && draftTotal > 0 && newestDraftId !== undefined
          ? { run: newestDraftId }
          : undefined,
      cta: importCta,
    },
    {
      key: 'review-tags',
      title: 'Review your tags',
      detail: reviewDetail,
      state: reviewState,
      to: '/lists',
      cta: reviewCta,
    },
    {
      key: 'triage',
      title: 'Triage',
      detail: triageDetail,
      state: triageState,
      to: '/triage',
      cta: hasContacts && untriaged === 0 ? 'Open triage' : 'Continue triage',
    },
    {
      key: 'build-list',
      title: 'Build a list',
      detail: listDetail,
      state: null,
      to: '/lists',
      cta: 'Open lists',
    },
    {
      key: 'export',
      title: 'Export',
      detail: exportDetail,
      state: exportState,
      to: '/exports',
      cta: 'Export contacts',
    },
  ]
}
