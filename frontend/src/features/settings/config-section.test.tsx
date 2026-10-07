import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import type { ConfigField, ConfigSettings } from './api'
import { draftProblem, valueOf, warnAboveHint } from './config-fields'
import { ConfigSections } from './config-section'

function field(overrides: Partial<ConfigField>): ConfigField {
  return {
    key: 'linkedin.budget.li_prefills_per_day',
    group: 'linkedin_budgets',
    label: 'LinkedIn prefills a day',
    help: 'How many campaign messages netkeeper may type.',
    kind: 'int',
    applies: 'now',
    applies_note: 'Applies from the next LinkedIn run.',
    minimum: 1,
    maximum: 50,
    warn_above: 20,
    value: 15,
    default: 15,
    ui_value: null,
    file_value: null,
    source: 'default',
    editable: true,
    locked_reason: null,
    restart_pending: false,
    stored_unreadable: false,
    warnings: [],
    notes: [],
    ...overrides,
  }
}

const PREFILLS = field({})
const VISITS = field({
  key: 'linkedin.budget.profile_visits_per_day',
  label: 'Profile visits a day',
  maximum: 250,
  warn_above: 100,
  value: 40,
  default: 60,
  file_value: 40,
  source: 'file',
  editable: false,
  locked_reason: 'Set in /etc/nk.toml, which wins over this page.',
})
const AUTO_SEND = field({
  key: 'campaigns.linkedin_auto_send',
  group: 'campaigns',
  label: 'LinkedIn auto-send',
  kind: 'bool',
  minimum: null,
  maximum: null,
  warn_above: null,
  value: false,
  default: false,
  editable: false,
  locked_reason: 'Only config.toml can change it.',
})
const POLL = field({
  key: 'campaigns.reply_poll_minutes',
  group: 'campaigns',
  label: 'Check Gmail for replies every (minutes)',
  applies: 'restart',
  maximum: 1440,
  warn_above: null,
  value: 5,
  default: 10,
  ui_value: 5,
  source: 'ui',
  restart_pending: true,
})

function renderSections(fields: ConfigField[] = [PREFILLS, VISITS, AUTO_SEND, POLL]) {
  const puts: Record<string, unknown>[] = []
  let current: ConfigSettings = { config_path: '/etc/nk.toml', fields }
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/posture') return jsonResponse({}, 500)
    if (pathname !== '/api/v1/settings/config') return jsonResponse({}, 500)
    if (request.method === 'GET') return jsonResponse(current)
    expect(request.headers.get('X-Netkeeper-Client')).not.toBeNull()
    const { values } = (await request.json()) as { values: Record<string, unknown> }
    puts.push(values)
    current = {
      ...current,
      fields: current.fields.map((f) =>
        f.key in values
          ? {
              ...f,
              value: values[f.key] ?? f.default,
              source: values[f.key] === null ? 'default' : 'ui',
              notes:
                f.key === PREFILLS.key && Number(values[f.key]) > 20
                  ? ['LinkedIn prefills are set to 30 a day, above 20 a day.']
                  : [],
            }
          : f,
      ),
    }
    return jsonResponse(current)
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ConfigSections />
    </QueryClientProvider>,
  )
  return { puts }
}

describe('ConfigSections', () => {
  it('shows where each value comes from, and locks what config.toml sets', async () => {
    renderSections()
    const budgets = within(await screen.findByRole('form', { name: 'LinkedIn budgets' }))
    const prefills = within(budgets.getByRole('group', { name: 'LinkedIn prefills a day' }))
    expect(prefills.getByText('Default')).toBeVisible()
    expect(prefills.getByText(/Hard maximum 50/)).toBeVisible()
    const visits = within(budgets.getByRole('group', { name: 'Profile visits a day' }))
    expect(visits.getByText('config.toml')).toBeVisible()
    expect(visits.getByRole('spinbutton')).toBeDisabled()
    expect(visits.getByText(/Set in \/etc\/nk.toml, which wins/)).toBeVisible()
  })

  it('never offers auto-send as a switch it can turn on', async () => {
    renderSections()
    const campaigns = within(await screen.findByRole('form', { name: 'Campaign defaults' }))
    expect(campaigns.getByRole('checkbox', { name: 'LinkedIn auto-send' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(campaigns.getByText('Only config.toml can change it.')).toBeVisible()
  })

  it('warns above 20 before saving, saves only what changed, and shows the note after', async () => {
    const { puts } = renderSections()
    const budgets = within(await screen.findByRole('form', { name: 'LinkedIn budgets' }))
    const prefills = within(budgets.getByRole('group', { name: 'LinkedIn prefills a day' }))
    fireEvent.change(prefills.getByRole('spinbutton'), { target: { value: '30' } })
    expect(prefills.getByRole('note')).toHaveTextContent(/Above 20 a day, LinkedIn is more likely/)
    fireEvent.click(budgets.getByRole('button', { name: 'Save linkedin budgets' }))
    await waitFor(() => expect(puts).toEqual([{ [PREFILLS.key]: 30 }]))
    expect(await screen.findByText(/LinkedIn prefills are set to 30 a day/)).toBeVisible()
  })

  it('refuses a value above the hard maximum before saving', async () => {
    const { puts } = renderSections()
    const budgets = within(await screen.findByRole('form', { name: 'LinkedIn budgets' }))
    const prefills = within(budgets.getByRole('group', { name: 'LinkedIn prefills a day' }))
    fireEvent.change(prefills.getByRole('spinbutton'), { target: { value: '51' } })
    expect(prefills.getByRole('alert')).toHaveTextContent('The hard maximum is 50.')
    expect(budgets.getByRole('button', { name: 'Save linkedin budgets' })).toBeDisabled()
    expect(puts).toEqual([])
  })

  it('says a restart is needed, and resets a value to its default', async () => {
    const { puts } = renderSections()
    const campaigns = within(await screen.findByRole('form', { name: 'Campaign defaults' }))
    const poll = within(
      campaigns.getByRole('group', { name: 'Check Gmail for replies every (minutes)' }),
    )
    expect(poll.getByText(/Restart netkeeper serve to apply this value/)).toBeVisible()
    fireEvent.click(poll.getByRole('button', { name: 'Use the default (10)' }))
    await waitFor(() => expect(puts).toEqual([{ [POLL.key]: null }]))
  })

  it('shows the warnings the value in force earns', async () => {
    renderSections([field({ warnings: ['the window runs overnight'] })])
    expect(await screen.findByText('Warning: the window runs overnight')).toBeVisible()
  })
})

describe('config field helpers', () => {
  it('checks numbers, windows and dates', () => {
    expect(draftProblem(PREFILLS, '0')).toBe('The least is 1.')
    expect(draftProblem(PREFILLS, '2.5')).toBe('Enter a whole number.')
    expect(draftProblem(PREFILLS, '50')).toBeNull()
    const window = field({ kind: 'window', minimum: null, maximum: null })
    expect(draftProblem(window, ['09:00', '09:00'])).toBe('The start and end must differ.')
    expect(draftProblem(window, ['9:00', '17:00'])).toBe('Enter both times as HH:MM.')
    const dates = field({ kind: 'dates', minimum: null, maximum: null })
    expect(draftProblem(dates, '2026-12-25\n12/26')).toBe('12/26 is not a date as YYYY-MM-DD.')
    expect(valueOf(dates, '2026-12-25, 2026-12-26')).toEqual(['2026-12-25', '2026-12-26'])
    const week = field({ kind: 'optional_int', warn_above: null })
    expect(draftProblem(week, '')).toBeNull()
    expect(valueOf(week, '')).toBeNull()
  })

  it('hints only above warn_above', () => {
    expect(warnAboveHint(PREFILLS, '20')).toBeNull()
    expect(warnAboveHint(PREFILLS, '21')).not.toBeNull()
  })
})
