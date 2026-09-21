/**
 * The setup path: what each step is, its real count, and whether it is done,
 * in progress, or not started.
 *
 * A pure function so the state logic is tested without rendering anything,
 * and so a later phase adds its own step (connect LinkedIn in phase 2,
 * connect Gmail in phase 3) by appending one more entry to the array this
 * builds, not by reshaping the page around it.
 *
 * Every count below comes from an endpoint that already exists (issue #115,
 * scope item 2). Two of the five steps have no such endpoint to ask "is this
 * done": nothing records that a person looked at an auto-tag, or that an
 * export ran (exports are a browser download, not a tracked action). Rather
 * than invent a signal for those, their state is `'unknown'` once there is
 * something to act on — a fourth, honest state, distinct from the three the
 * issue asks for the knowable steps.
 */
import type { ContactStats, ImportRunPage } from './api'
import type { ListOut } from '@/features/crm/types'

export type StepState = 'not_started' | 'in_progress' | 'done' | 'unknown'

export type StepRoute = '/imports' | '/triage' | '/lists' | '/exports'

export interface SetupStep {
  key: string
  title: string
  /** What the step's real count says right now. */
  detail: string
  state: StepState
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
  /** True when the lists query itself failed, so the list step says so rather than guessing "not started". */
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

  const draftTotal = openImports?.total ?? 0
  const newestDraftId = openImports?.items[0]?.id
  const draftsKnown = !openImportsUnavailable

  const importState: StepState =
    draftsKnown && draftTotal > 0
      ? 'in_progress'
      : !draftsKnown
        ? 'unknown'
        : hasContacts
          ? 'done'
          : 'not_started'
  const importDetail = !draftsKnown
    ? hasContacts
      ? `${plural(total, 'contact')} imported; open imports could not be checked`
      : 'Open imports could not be checked'
    : draftTotal > 0
      ? `${plural(draftTotal, 'draft import')} waiting to be finished`
      : hasContacts
        ? `${plural(total, 'contact')} imported`
        : 'Nothing imported yet'
  const importCta = draftsKnown && draftTotal > 0 ? 'Continue this import' : 'Import contacts'

  const tagged = stats?.tagged ?? 0
  const reviewState: StepState = hasContacts ? 'unknown' : 'not_started'
  const reviewDetail = hasContacts
    ? `${plural(tagged, 'contact')} tagged automatically`
    : 'Nothing tagged yet'

  const untriaged = stats?.untriaged ?? 0
  const triagedCount = Math.max(total - untriaged, 0)
  const triageState: StepState = !hasContacts
    ? 'not_started'
    : untriaged === 0
      ? 'done'
      : 'in_progress'
  const triageDetail = hasContacts ? `${triagedCount} of ${total} triaged` : 'Nothing to triage yet'

  const listsKnown = !listsUnavailable
  const listCount = lists?.length ?? 0
  const listState: StepState = !listsKnown
    ? 'unknown'
    : hasContacts && listCount > 0
      ? 'done'
      : 'not_started'
  const listDetail = !listsKnown
    ? 'List count could not be checked'
    : listCount > 0
      ? `${plural(listCount, 'list')} built`
      : 'No lists yet'

  const exportState: StepState = hasContacts ? 'unknown' : 'not_started'
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
      title: 'Review what was tagged automatically',
      detail: reviewDetail,
      state: reviewState,
      to: '/lists',
      cta: 'Review tags',
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
      state: listState,
      to: '/lists',
      cta: listCount > 0 ? 'Open lists' : 'Build a list',
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
