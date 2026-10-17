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

function settings(fields: ConfigField[], overrides: Partial<ConfigSettings> = {}): ConfigSettings {
  return {
    config_path: '/etc/nk.toml',
    log_file: '/data/nk/logs/netkeeper.log',
    config_changed: false,
    config_error: null,
    restart_pending: fields.some((f) => f.restart_pending),
    can_restart: true,
    busy: [],
    restart_waiting: false,
    fields,
    ...overrides,
  }
}

function renderSections(
  fields: ConfigField[] = [PREFILLS, VISITS, AUTO_SEND, POLL],
  overrides: Partial<ConfigSettings> = {},
) {
  const puts: Record<string, unknown>[] = []
  const restarts: string[] = []
  const restartBodies: unknown[] = []
  const cancels: string[] = []
  let boot = 'boot-1'
  let current: ConfigSettings = settings(fields, overrides)
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/posture') return jsonResponse({}, 500)
    if (pathname === '/api/v1/settings/restart' && request.method === 'DELETE') {
      cancels.push('DELETE')
      current = { ...current, restart_waiting: false }
      return jsonResponse({ cancelled: true })
    }
    if (pathname === '/api/v1/settings/restart') {
      expect(request.method).toBe('POST')
      expect(request.headers.get('X-Netkeeper-Client')).not.toBeNull()
      restarts.push(boot)
      const stopping = boot
      boot = 'boot-2'
      // The restarted server applies the pending value: nothing is pending after.
      current = settings(
        current.fields.map((f) => ({ ...f, restart_pending: false })),
        { ...overrides, config_changed: false },
      )
      restartBodies.push(await request.json())
      return jsonResponse({ mode: 'exec', boot_id: stopping, waiting_on: [] }, 202)
    }
    if (pathname === '/api/v1/health') {
      return jsonResponse({ status: 'ok', version: 'test', boot_id: boot })
    }
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
  return { puts, restarts, restartBodies, cancels }
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

  it('says where the log file is in the Logs card, and saves its limits', async () => {
    const size = field({
      key: 'logs.max_mb',
      group: 'logs',
      label: 'Log file size (MB)',
      maximum: 1000,
      warn_above: null,
      value: 10,
      default: 10,
    })
    const { puts } = renderSections([PREFILLS, size])
    const logs = within(await screen.findByRole('form', { name: 'Logs' }))
    expect(screen.getByText('/data/nk/logs/netkeeper.log')).toBeVisible()
    expect(screen.getAllByText('/data/nk/logs/netkeeper.log')).toHaveLength(1)
    fireEvent.change(logs.getByRole('spinbutton'), { target: { value: '25' } })
    fireEvent.click(logs.getByRole('button', { name: 'Save logs' }))
    await waitFor(() => expect(puts).toEqual([{ 'logs.max_mb': 25 }]))
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
    expect(poll.getByText(/Restart netkeeper to apply this value/)).toBeVisible()
    fireEvent.click(poll.getByRole('button', { name: 'Use the default (10)' }))
    await waitFor(() => expect(puts).toEqual([{ [POLL.key]: null }]))
  })

  it('marks a restart-only field, and offers Restart netkeeper until serve is back', async () => {
    const { restarts, restartBodies } = renderSections()
    const campaigns = within(await screen.findByRole('form', { name: 'Campaign defaults' }))
    const poll = within(
      campaigns.getByRole('group', { name: 'Check Gmail for replies every (minutes)' }),
    )
    expect(poll.getByText('Needs a restart')).toBeVisible()
    const banner = within(screen.getByRole('region', { name: 'Restart netkeeper' }))
    expect(banner.getByText(/A setting you changed takes effect when netkeeper restarts/))
    fireEvent.click(banner.getByRole('button', { name: 'Restart netkeeper' }))
    expect(await screen.findByText(/Restarting… this page reconnects/)).toBeVisible()
    expect(
      await screen.findByText(
        'netkeeper restarted, and the settings above are in force.',
        {},
        { timeout: 3_000 },
      ),
    ).toBeVisible()
    expect(restarts).toEqual(['boot-1'])
    expect(restartBodies).toEqual([{ when_idle: false }])
    expect(screen.queryByRole('region', { name: 'Restart netkeeper' })).toBeNull()
  })

  it('names what is running, and offers Restart when idle instead', async () => {
    const { restartBodies } = renderSections([PREFILLS], {
      restart_pending: true,
      busy: ['a LinkedIn enrich run (#7) is going; cancel it or wait for it to finish'],
    })
    const banner = within(await screen.findByRole('region', { name: 'Restart netkeeper' }))
    expect(banner.getByText('netkeeper is busy:')).toBeVisible()
    expect(banner.getByText(/a LinkedIn enrich run \(#7\) is going/)).toBeVisible()
    expect(banner.queryByRole('button', { name: 'Restart netkeeper' })).toBeNull()
    fireEvent.click(banner.getByRole('button', { name: 'Restart when idle' }))
    await waitFor(() => expect(restartBodies).toEqual([{ when_idle: true }]))
  })

  it('shows a restart that waits for idle, and cancels it', async () => {
    const { cancels } = renderSections([PREFILLS], {
      restart_pending: true,
      restart_waiting: true,
      busy: ['a campaign send is in progress'],
    })
    const banner = within(await screen.findByRole('region', { name: 'Restart netkeeper' }))
    expect(banner.getByText('netkeeper restarts once this is done:')).toBeVisible()
    expect(banner.getByText(/nothing running is cut off/)).toBeVisible()
    expect(banner.getByText(/A run you start by hand after that keeps them paused/)).toBeVisible()
    expect(banner.queryByRole('button', { name: /Restart/ })).toBeNull()
    fireEvent.click(banner.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(cancels).toEqual(['DELETE']))
  })

  it('shows no restart banner when nothing waits for one', async () => {
    renderSections([PREFILLS])
    await screen.findByRole('form', { name: 'LinkedIn budgets' })
    expect(screen.queryByRole('region', { name: 'Restart netkeeper' })).toBeNull()
  })

  it('says config.toml changed, and will not restart onto a file that does not load', async () => {
    renderSections([PREFILLS], {
      config_changed: true,
      restart_pending: true,
      config_error: '/etc/nk.toml: invalid TOML',
    })
    const banner = within(await screen.findByRole('region', { name: 'Restart netkeeper' }))
    expect(banner.getByText(/config.toml changed since netkeeper started/)).toBeVisible()
    expect(banner.getByRole('alert')).toHaveTextContent('/etc/nk.toml: invalid TOML')
    expect(banner.getByRole('button', { name: 'Restart netkeeper' })).toBeDisabled()
  })

  it('says how to restart when serve cannot restart itself', async () => {
    renderSections([PREFILLS], { restart_pending: true, can_restart: false })
    const banner = within(await screen.findByRole('region', { name: 'Restart netkeeper' }))
    expect(banner.queryByRole('button')).toBeNull()
    expect(banner.getByText(/Stop it and start it again/)).toBeVisible()
  })

  it('edits a text field and saves it trimmed', async () => {
    const zone = field({
      key: 'linkedin.timezone',
      group: 'linkedin_hours',
      label: 'Time zone',
      kind: 'text',
      minimum: null,
      maximum: null,
      warn_above: null,
      value: 'America/New_York',
      default: 'America/New_York',
    })
    const { puts } = renderSections([zone])
    const hours = within(await screen.findByRole('form', { name: 'LinkedIn active hours' }))
    const input = hours.getByRole('textbox', { name: 'Time zone' })
    fireEvent.change(input, { target: { value: '' } })
    expect(hours.getByRole('alert')).toHaveTextContent('Enter a value.')
    fireEvent.change(input, { target: { value: ' Europe/Berlin ' } })
    fireEvent.click(hours.getByRole('button', { name: 'Save linkedin active hours' }))
    await waitFor(() => expect(puts).toEqual([{ 'linkedin.timezone': 'Europe/Berlin' }]))
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
