/**
 * Every call the Settings page makes, through the generated client.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

export type Posture = components['schemas']['PostureOut']
export type Protection = components['schemas']['ProtectionOut']
export type DoNotSendEntry = components['schemas']['DoNotSendOut']
export type SendingHours = components['schemas']['SendingHoursOut']
export type SendingHoursIn = components['schemas']['SendingHoursIn']
export type Day = SendingHoursIn['days'][number]
export type SelfContact = components['schemas']['SelfContactOut']
export type SelfContactIn = components['schemas']['SelfContactIn']

/**
 * Every protection the LinkedIn extractor has (spec section 9, P2-11), read-only.
 *
 * Built with no browser probe (`GET /posture` never attaches, CLAUDE.md), so
 * the `linkedin session` row always reads "unknown" here — a live check stays
 * `netkeeper preflight`, a terminal command.
 */
export const postureQuery = queryOptions({
  queryKey: ['posture'] as const,
  queryFn: async ({ signal }): Promise<Posture> => {
    const { data, error, response } = await api.GET('/api/v1/posture', { signal })
    if (data === undefined) {
      const detail = (error as { detail?: unknown } | undefined)?.detail
      const message = typeof detail === 'string' && detail !== '' ? detail : null
      throw new Error(message ?? `GET /api/v1/posture returned ${response.status}`)
    }
    return data
  },
})

function failure(error: unknown, response: Response, what: string): Error {
  return new Error(detailMessage(error) ?? `${what} returned ${response.status}`)
}

/** The posture row's `key` whose warning **Acknowledge** clears (#383). */
export const REPLY_POLL_KEY = 'linkedin_reply_poll'
/** The `key` of ADR 0004's manual LinkedIn sends row: off means auto-send is on. */
export const MANUAL_SENDS_KEY = 'manual_linkedin_sends'

/**
 * You checked older LinkedIn replies by hand: clear the warning that the first inbox
 * poll could not read back far enough (`netkeeper linkedin inbox-acknowledge`). False
 * when there was nothing to clear.
 */
export async function acknowledgeInboxFirstPoll(): Promise<boolean> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/inbox/acknowledge')
  if (data === undefined) {
    throw failure(error, response, 'POST /api/v1/linkedin/inbox/acknowledge')
  }
  return data.cleared
}

/** A posture row's action (#583): **Reset breaker**, on a tripped breaker's row. */
export const RESET_BREAKER_ACTION = 'reset_breaker'
/** A posture row's action (#583): **Forget inbox owner**, where an owner mismatch shows. */
export const FORGET_INBOX_OWNER_ACTION = 'forget_inbox_owner'

/**
 * Reset the tripped LinkedIn breakers (`netkeeper linkedin schedule reset-breaker`),
 * which clears all five. `tripped` is the keys of the rows offering the reset that the
 * person was shown: the backend refuses (409) when a different set is tripped now, or none.
 */
export async function resetBreaker(tripped: readonly string[]): Promise<void> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/schedule/reset-breaker', {
    body: { confirm: true, tripped: [...tripped] },
  })
  if (data === undefined) {
    throw failure(error, response, 'POST /api/v1/linkedin/schedule/reset-breaker')
  }
}

/**
 * Forget the LinkedIn mailbox the inbox poll recorded (`netkeeper linkedin
 * inbox-forget-owner`). The backend refuses (409) when none is recorded.
 */
export async function forgetInboxOwner(): Promise<void> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/inbox/forget-owner', {
    body: { confirm: true },
  })
  if (data === undefined) {
    throw failure(error, response, 'POST /api/v1/linkedin/inbox/forget-owner')
  }
}

/** The addresses no campaign sends to (#238), newest first. */
export const doNotSendQuery = queryOptions({
  queryKey: ['do-not-send'] as const,
  queryFn: async ({ signal }): Promise<DoNotSendEntry[]> => {
    const { data, error, response } = await api.GET('/api/v1/do-not-send', { signal })
    if (data === undefined) throw failure(error, response, 'GET /do-not-send')
    return data
  },
})

/** Take an address off the list: a person's explicit action, so campaigns may send to it. */
export async function removeDoNotSend(entryId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/do-not-send/{entry_id}', {
    params: { path: { entry_id: entryId } },
  })
  if (!response.ok) throw failure(error, response, 'DELETE /do-not-send/{id}')
}

/**
 * The sending hours (#338): when campaign email may go out, after each campaign's start.
 * A global setting, stored in the database, so it is changed here and not in a file.
 */
export const sendingHoursQuery = queryOptions({
  queryKey: ['sending-hours'] as const,
  queryFn: async ({ signal }): Promise<SendingHours> => {
    const { data, error, response } = await api.GET('/api/v1/settings/sending-hours', { signal })
    if (data === undefined) throw failure(error, response, 'GET /settings/sending-hours')
    return data
  },
})

/** Replace the sending hours; 422 for no day, or an end that is not after the start. */
export async function saveSendingHours(body: SendingHoursIn): Promise<SendingHours> {
  const { data, error, response } = await api.PUT('/api/v1/settings/sending-hours', { body })
  if (data === undefined) throw failure(error, response, 'PUT /settings/sending-hours')
  return data
}

/**
 * Your own details, held as the self contact (#342): what a test send renders a
 * template's contact fields with. Never in your contact lists or campaigns.
 */
export const selfContactQuery = queryOptions({
  queryKey: ['self-contact'] as const,
  queryFn: async ({ signal }): Promise<SelfContact> => {
    const { data, error, response } = await api.GET('/api/v1/settings/self-contact', { signal })
    if (data === undefined) throw failure(error, response, 'GET /settings/self-contact')
    return data
  },
})

/** Replace your own details; 422 for a value that is too long. */
export async function saveSelfContact(body: SelfContactIn): Promise<SelfContact> {
  const { data, error, response } = await api.PUT('/api/v1/settings/self-contact', { body })
  if (data === undefined) throw failure(error, response, 'PUT /settings/self-contact')
  return data
}

export type ConfigSettings = components['schemas']['ConfigOut']
export type ConfigField = components['schemas']['ConfigFieldOut']

/**
 * The settings you change here instead of in `config.toml` (#343): each with its value
 * in force, where it comes from (default, Settings or config.toml, which wins), when a
 * change applies, and the warnings and notes the value earns.
 */
export const configSettingsQuery = queryOptions({
  queryKey: ['config-settings'] as const,
  queryFn: async ({ signal }): Promise<ConfigSettings> => {
    const { data, error, response } = await api.GET('/api/v1/settings/config', { signal })
    if (data === undefined) throw failure(error, response, 'GET /settings/config')
    return data
  },
})

/**
 * Store new values, all or none: `null` goes back to the default. 422 names each value
 * refused (above its hard maximum, a key config.toml sets, and so on).
 */
export async function saveConfigSettings(values: Record<string, unknown>): Promise<ConfigSettings> {
  const { data, error, response } = await api.PUT('/api/v1/settings/config', {
    body: { values },
  })
  if (data === undefined) throw failure(error, response, 'PUT /settings/config')
  return data
}

export type Restart = components['schemas']['RestartOut']

/**
 * Restart netkeeper serve (#597) so the settings it reads at startup apply. 409 when it
 * cannot restart itself, config.toml no longer loads, or something is running (a
 * LinkedIn run, the browser, a task, a campaign send) and `whenIdle` is not set; with
 * it, serve restarts once that work is done.
 */
export async function restartServe(whenIdle = false): Promise<Restart> {
  const { data, error, response } = await api.POST('/api/v1/settings/restart', {
    body: { when_idle: whenIdle },
  })
  if (data === undefined) throw failure(error, response, 'POST /settings/restart')
  return data
}

/** Cancel a restart that waits for idle; false when nothing waited. */
export async function cancelRestart(): Promise<boolean> {
  const { data, error, response } = await api.DELETE('/api/v1/settings/restart')
  if (data === undefined) throw failure(error, response, 'DELETE /settings/restart')
  return data.cancelled
}

/** The running server's boot id, or null while nothing answers (it is restarting). */
export async function currentBootId(): Promise<string | null> {
  try {
    const { data } = await api.GET('/api/v1/health')
    return data?.boot_id ?? null
  } catch {
    return null
  }
}

export type Backups = components['schemas']['BackupsOut']
export type Backup = components['schemas']['BackupOut']

/** The database's backups (P6-04), newest first. */
export const backupsQuery = queryOptions({
  queryKey: ['backups'] as const,
  queryFn: async ({ signal }): Promise<Backups> => {
    const { data, error, response } = await api.GET('/api/v1/backups', { signal })
    if (data === undefined) throw failure(error, response, 'GET /backups')
    return data
  },
})

/** Back up the database now; older backups past `backup.keep` are deleted. */
export async function createBackup(): Promise<Backups> {
  const { data, error, response } = await api.POST('/api/v1/backups')
  if (data === undefined) throw failure(error, response, 'POST /backups')
  return data
}
