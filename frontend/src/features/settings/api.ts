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
