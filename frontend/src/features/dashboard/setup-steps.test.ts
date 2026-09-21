/**
 * `buildSetupSteps` is the state logic behind the dashboard (issue #115): a
 * pure function of the API responses, so every state it can land on — done,
 * in progress, not started, and the honest fourth "not tracked" — is tested
 * without rendering anything.
 */
import { describe, expect, it } from 'vitest'

import type { ContactStats, ImportRunPage } from './api'
import { buildSetupSteps } from './setup-steps'
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
    ...overrides,
  }
}

function list(id: number): ListOut {
  return {
    id,
    name: `List ${id}`,
    kind: 'static',
    filter: null,
    member_count: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }
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
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    committed_at: null,
    rolled_back_at: null,
  }
}

function byKey(steps: ReturnType<typeof buildSetupSteps>, key: string) {
  const step = steps.find((candidate) => candidate.key === key)
  if (step === undefined) throw new Error(`no step ${key}`)
  return step
}

describe('buildSetupSteps: the empty state', () => {
  it('tells a fresh install to import first, and marks every later step not started', () => {
    const steps = buildSetupSteps({ stats: stats() })

    expect(byKey(steps, 'import')).toMatchObject({ state: 'not_started', cta: 'Import contacts' })
    expect(byKey(steps, 'review-tags').state).toBe('not_started')
    expect(byKey(steps, 'triage').state).toBe('not_started')
    expect(byKey(steps, 'build-list').state).toBe('not_started')
    expect(byKey(steps, 'export').state).toBe('not_started')
  })

  it('always returns the five steps in setup order', () => {
    const steps = buildSetupSteps({ stats: stats() })
    expect(steps.map((step) => step.key)).toEqual([
      'import',
      'review-tags',
      'triage',
      'build-list',
      'export',
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

  it('reports "not tracked" rather than a false "done" when the draft query itself fails', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 5 }), openImportsUnavailable: true })
    expect(byKey(steps, 'import').state).toBe('unknown')
  })
})

describe('buildSetupSteps: review tags', () => {
  it('has no way to know "reviewed", so it never claims done — only the tagged count', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10, tagged: 4 }) })
    const step = byKey(steps, 'review-tags')
    expect(step.state).toBe('unknown')
    expect(step.detail).toBe('4 contacts tagged automatically')
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
  it('is done once a list exists', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10 }), lists: [list(1), list(2)] })
    expect(byKey(steps, 'build-list')).toMatchObject({ state: 'done', detail: '2 lists built' })
  })

  it('is not started with contacts but no lists', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10 }), lists: [] })
    expect(byKey(steps, 'build-list').state).toBe('not_started')
  })

  it('reports "not tracked" rather than a false "not started" when the lists query fails', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10 }), listsUnavailable: true })
    expect(byKey(steps, 'build-list').state).toBe('unknown')
  })
})

describe('buildSetupSteps: export', () => {
  it('has no tracked signal at all once there is something to export', () => {
    const steps = buildSetupSteps({ stats: stats({ total: 10 }) })
    expect(byKey(steps, 'export').state).toBe('unknown')
  })

  it('is not started with nothing imported', () => {
    const steps = buildSetupSteps({ stats: stats() })
    expect(byKey(steps, 'export').state).toBe('not_started')
  })
})
