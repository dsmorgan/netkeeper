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
 * progress — not a system fact about them. A step whose completion nothing
 * can verify does not get a fourth state wedged into that ladder — its
 * `state` is `null` (no badge), and its `detail` says the true thing in
 * words instead. Three steps land there once there are contacts to act on:
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
 *   There is no fix within this endpoint's response shape; tracked as its own
 *   backend issue.
 * - **Review what was tagged by a rule**, once `stats.tagged_by_rule`
 *   (`TagSource.RULE` only, distinct from `tagged`, which counts any source,
 *   and from `TagSource.LLM`, which this step also does not read — nothing
 *   writes it yet, but "automatically" would already be the wrong word for a
 *   count that deliberately excludes it) is above zero, is the same conflation as
 *   build-a-list's, just on the other side: `tagged_by_rule > 0` is a fact
 *   about the rules, not about whether the person has reviewed what the
 *   rules did — and the step's own title is an instruction to *them*. Sharp
 *   case: a 200-contact import that auto-tags 150 must not render "Done"
 *   before anyone has opened the app. So `tagged_by_rule === 0` is a real
 *   `not_started` (the one direction a badge can honestly claim — there is
 *   categorically nothing to review yet, and "run the rules" is a concrete
 *   next action), but `tagged_by_rule > 0` is `null`, the same as export and
 *   build-a-list, with the real count in the detail line instead of a claim
 *   nobody reviewed anything.
 */
import type { ContactStats, ImportRunPage } from './api'
import type { CrmSearch } from '@/features/crm/crm-tabs'
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
  /** The `/lists` tab the step's control is about, when it is not the first one. */
  tab?: CrmSearch['tab']
  cta: string
}

export interface SetupStepInputs {
  /** `undefined` while pending or on error — the page gates on this before rendering steps. */
  stats: ContactStats | undefined
  /** `undefined` when the draft-imports query has not answered; drafts are then assumed unknown. */
  openImports?: ImportRunPage
  /**
   * True before the draft-imports query has answered for the first time.
   * `contacts/stats` gates the page, but the drafts query is its own fetch
   * on its own clock — without this, the window between the two answering
   * renders "Done" (no drafts known yet reads the same as none existing),
   * which is exactly the wrong claim the query exists to prevent.
   */
  openImportsPending?: boolean
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
  openImportsPending = false,
  openImportsUnavailable = false,
  lists,
  listsUnavailable = false,
}: SetupStepInputs): SetupStep[] {
  const total = stats?.total ?? 0
  const hasContacts = total > 0

  // --- import ---------------------------------------------------------------

  const draftsKnown = !openImportsPending && !openImportsUnavailable
  const draftTotal = draftsKnown ? (openImports?.total ?? 0) : 0
  const newestDraftId = openImports?.items[0]?.id

  const importState: StepState | null = openImportsPending
    ? null
    : !draftsKnown
      ? null
      : draftTotal > 0
        ? 'in_progress'
        : hasContacts
          ? 'done'
          : 'not_started'
  const importDetail = openImportsPending
    ? hasContacts
      ? `${plural(total, 'contact')} imported; checking for open imports…`
      : 'Checking for open imports…'
    : !draftsKnown
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
  // `tagged_by_rule === 0` is a real `not_started`: there is categorically
  // nothing to review yet, and "run the rules" is the concrete action. Above
  // zero is not `done` — that count says the rules ran, not that the person
  // reviewed what they did, and this step's title asks *them* to (see module
  // doc) — so it is `null`, the honest count with no badge, same as export
  // and build-a-list.

  const taggedByRule = stats?.tagged_by_rule ?? 0
  const reviewNotStarted = !hasContacts || taggedByRule === 0
  const reviewState: StepState | null = reviewNotStarted ? 'not_started' : null
  const reviewDetail = reviewNotStarted
    ? 'Nothing tagged yet'
    : `${plural(taggedByRule, 'contact')} tagged by a rule`
  const reviewCta = hasContacts && taggedByRule === 0 ? 'Run auto-tag rules' : 'Review tags'

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
  const listCta = listCount > 0 ? 'Open lists' : 'Build a list'

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
      title: 'Review what was tagged by a rule',
      detail: reviewDetail,
      state: reviewState,
      to: '/lists',
      // The control names the rules, and "Run all rules now" is on this tab.
      tab: 'tags',
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
      cta: listCta,
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
