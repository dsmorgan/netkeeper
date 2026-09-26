/**
 * `buildSetupSteps` is the state logic behind the dashboard (issue #115): a
 * pure function of the API responses, so every state it can land on — done,
 * in progress, not started, and the no-badge case where nothing can verify
 * completion — is tested without rendering anything.
 *
 * PR #129's review found five mutations (retitling a step, blanking a title,
 * repointing review-tags', triage's, or build-list's route) that left every
 * test here green, because nothing asserted a step's `title`, `to`, or `cta`
 * except import's. `shape()` below reads all three off every step, and the
 * "renders the real five steps" tests assert the whole array with `toEqual`,
 * so a wrong title or a swapped destination fails here, not just in a visual
 * review.
 */
import { describe, expect, it } from 'vitest'

import type { ContactStats, ImportRunPage } from './api'
import { buildSetupSteps, type SetupStep } from './setup-steps'
import type { ListOut } from '@/features/crm/types'

function stats(overrides: Partial<ContactStats> = {}): ContactStats {
  return {
    total: 0,
    met: 0,
    not_met: 0,
    skipped: 0,
    untriaged: 0,
    archived: 0,
    merged_away: 0,
    with_email: 0,
    with_phone: 0,
    tagged: 0,
    tagged_by_rule: 0,
    ...overrides,
  }
}

function list(id: number, overrides: Partial<ListOut> = {}): ListOut {
  return {
    id,
    name: `List ${id}`,
    kind: 'static',
    filter: null,
    member_count: 0,
    builtin: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

/**
 * `GET /api/v1/lists` on a real server: `ensure_validated_list` seeds this
 * smart list at every start (`netkeeper/web/app.py`), marked `builtin` (#133).
 * A fixture that defaults to `[]` is the one PR #129's review caught the fake
 * diverging from the server on.
 */
function seededValidatedList(): ListOut {
  return list(1, {
    name: 'Validated',
    kind: 'smart',
    filter: { where: { op: 'eq', field: 'met', value: 'met' }, include_archived: false },
    builtin: true,
  })
}

function draftRun(id: number): ImportRunPage['items'][number] {
  return {
    id,
    filename: 'contacts.csv',
    preset: 'nine-column',
    mapping: {},
    source_kind: 'csv',
    status: 'draft',
    total_rows: 8,
    candidate_count: 1,
    matched_count: 3,
    created_count: 4,
    skipped_count: 0,
    // A draft has not run the rules yet; a committed run carries what they did.
    tagged_contacts: 0,
    tags_added: 0,
    tags_removed: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    committed_at: null,
    rolled_back_at: null,
  }
}

function byKey(steps: SetupStep[], key: string) {
  const step = steps.find((candidate) => candidate.key === key)
  if (step === undefined) throw new Error(`no step ${key}`)
  return step
}

/** Every field a person or a test can observe, for one `toEqual` per scenario. */
function shape(steps: SetupStep[]) {
  return steps.map(({ key, title, detail, state, to, cta }) => ({
    key,
    title,
    detail,
    state,
    to,
    cta,
  }))
}

describe('buildSetupSteps: the empty state', () => {
  it('renders the real five steps, in order, with their real titles, routes, and controls', () => {
    // The seeded list is present even here: it exists from the first server
    // start, before any contact does (finding 1). Build-a-list still reads
    // no badge, not "not started", because that count was never zero.
    const steps = buildSetupSteps({
      stats: stats(),
      openImports: { items: [], total: 0 },
      lists: [seededValidatedList()],
    })

    expect(shape(steps)).toEqual([
      {
        key: 'import',
        title: 'Import your data',
        detail: 'Nothing imported yet',
        state: 'not_started',
        to: '/imports',
        cta: 'Import contacts',
      },
      {
        key: 'review-tags',
        title: 'Review what was tagged by a rule',
        detail: 'Nothing tagged yet',
        state: 'not_started',
        to: '/lists',
        cta: 'Review tags',
      },
      {
        key: 'triage',
        title: 'Triage',
        detail: 'Nothing to triage yet',
        state: 'not_started',
        to: '/triage',
        cta: 'Continue triage',
      },
      {
        key: 'build-list',
        title: 'Build a list',
        detail: '1 list',
        state: null,
        to: '/lists',
        cta: 'Open lists',
      },
      {
        key: 'export',
        title: 'Export',
        detail: 'Nothing to export yet',
        state: 'not_started',
        to: '/exports',
        cta: 'Export contacts',
      },
    ])
  })
})

describe('buildSetupSteps: import', () => {
  it('is done once contacts exist and nothing is left mid-import', () => {
    const steps = buildSetupSteps({
      stats: stats({ total: 12 }),
      openImports: { items: [], total: 0 },
    })
    expect(byKey(steps, 'import')).toMatchObject({ state: 'done', detail: '12 contacts imported' })
  })

  it('is in progress, and links to the resume screen, while a draft is open', () => {
    const steps = buildSetupSteps({
      stats: stats({ total: 5 }),
      openImports: { items: [draftRun(42)], total: 1 },
    })
    expect(byKey(steps, 'import')).toMatchObject({
      state: 'in_progress',
      to: '/imports',
      search: { run: 42 },
      cta: 'Continue this import',
    })
  })

  it('says which draft it resumes when several are open', () => {
    const steps = buildSetupSteps({
      stats: stats({ total: 5 }),
      openImports: { items: [draftRun(9), draftRun(3)], total: 2 },
    })
    expect(byKey(steps, 'import')).toMatchObject({
      detail: '2 draft imports waiting to be finished — resuming the most recent',
      search: { run: 9 },
    })
  })

  it('shows no badge, not a false "done", when the draft query itself fails', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 5 }), openImportsUnavailable: true })
    expect(byKey(steps, 'import')).toMatchObject({
      state: null,
      detail: '5 contacts imported; open imports could not be checked',
    })
  })

  it('shows no badge while the draft query is still in flight — not a "done" the answer might contradict', () => {
    // contacts/stats gates the whole page, but the drafts query is its own
    // fetch on its own clock: without openImportsPending, the window between
    // the two answering renders "Done" for a contact count that might have
    // a draft still open, which is exactly what the drafts query exists to
    // catch (#129 review round 3, verified with a 400ms probe).
    const steps = buildSetupSteps({ stats: stats({ total: 5 }), openImportsPending: true })
    expect(byKey(steps, 'import')).toMatchObject({
      state: null,
      detail: '5 contacts imported; checking for open imports…',
    })
  })

  it('shows no badge while pending on a fresh install too', () => {
    const steps = buildSetupSteps({ stats: stats(), openImportsPending: true })
    expect(byKey(steps, 'import')).toMatchObject({
      state: null,
      detail: 'Checking for open imports…',
    })
  })
})

describe('buildSetupSteps: review tags', () => {
  it('is a real "not started" with contacts and nothing rule-tagged — a concrete, knowable next step', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10, tagged_by_rule: 0 }) })
    expect(byKey(steps, 'review-tags')).toMatchObject({
      state: 'not_started',
      detail: 'Nothing tagged yet',
      cta: 'Run auto-tag rules',
    })
  })

  it('is not fooled by a hand-applied tag: manual tagging alone is still "not started"', () => {
    // `tagged` (any source) is 5 — somebody tagged five contacts by hand — but
    // `tagged_by_rule` (TagSource.RULE only) is 0: no rule has ever run. This is
    // the exact case #129's review caught: a manual tag must not read as
    // automatic progress.
    const steps = buildSetupSteps({ stats: stats({ total: 10, tagged: 5, tagged_by_rule: 0 }) })
    expect(byKey(steps, 'review-tags')).toMatchObject({
      state: 'not_started',
      detail: 'Nothing tagged yet',
      cta: 'Run auto-tag rules',
    })
  })

  it('shows no badge once a rule has tagged someone — the count is a system fact, not that the person reviewed it', () => {
    // A rule ran and tagged five contacts (`tagged: 6` includes one hand-applied
    // on top). That is real progress by the rules, but not proof the person
    // reviewed what the rules did — the same conflation build-a-list already
    // avoids (#129 review round 3) — so this is `null`, not `done`, with the
    // real, rule-only count in the detail line.
    const steps = buildSetupSteps({ stats: stats({ total: 10, tagged: 6, tagged_by_rule: 5 }) })
    const step = byKey(steps, 'review-tags')
    expect(step.state).toBeNull()
    expect(step.detail).toBe('5 contacts tagged by a rule')
    expect(step.cta).toBe('Review tags')
    expect(step.title).toBe('Review what was tagged by a rule')
  })

  it('is the sharp case: a big auto-tagging import must not read "Done" before anyone opens the app', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 200, tagged_by_rule: 150 }) })
    expect(byKey(steps, 'review-tags')).toMatchObject({
      state: null,
      detail: '150 contacts tagged by a rule',
    })
  })
})

describe('buildSetupSteps: triage', () => {
  it('is in progress with contacts left untriaged', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10, untriaged: 3, met: 5, not_met: 2 }) })
    expect(byKey(steps, 'triage')).toMatchObject({
      state: 'in_progress',
      detail: '7 of 10 triaged',
    })
  })

  it('is done once nobody is left untriaged', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10, untriaged: 0, met: 8, not_met: 2 }) })
    expect(byKey(steps, 'triage')).toMatchObject({ state: 'done', detail: '10 of 10 triaged' })
  })
})

describe('buildSetupSteps: build a list', () => {
  it('never claims done or not started — GET /lists always includes the seeded "Validated" list', () => {
    const steps = buildSetupSteps({
      stats: stats({ total: 10 }),
      lists: [seededValidatedList(), list(2)],
    })
    expect(byKey(steps, 'build-list')).toMatchObject({
      state: null,
      detail: '2 lists',
      cta: 'Open lists',
    })
  })

  it('offers to build one, not "open lists", when the count is genuinely zero', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10 }), lists: [] })
    expect(byKey(steps, 'build-list')).toMatchObject({
      state: null,
      detail: '0 lists',
      cta: 'Build a list',
    })
  })

  it('shows no badge with contacts and only the seeded list — not a false "done"', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10 }), lists: [seededValidatedList()] })
    expect(byKey(steps, 'build-list')).toMatchObject({ state: null, detail: '1 list' })
  })

  it('shows no badge before any contact either — the seeded list predates import', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 0 }), lists: [seededValidatedList()] })
    expect(byKey(steps, 'build-list')).toMatchObject({ state: null, detail: '1 list' })
  })

  it('reports the count could not be checked, not a guess, when the lists query fails', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10 }), listsUnavailable: true })
    expect(byKey(steps, 'build-list')).toMatchObject({
      state: null,
      detail: 'List count could not be checked',
    })
  })
})

describe('buildSetupSteps: export', () => {
  it('has no tracked signal at all once there is something to export', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10 }) })
    expect(byKey(steps, 'export')).toMatchObject({ state: null })
  })

  it('is not started with nothing imported', () => {
    const steps = buildSetupSteps({ stats: stats() })
    expect(byKey(steps, 'export').state).toBe('not_started')
  })
})
