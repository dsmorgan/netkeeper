/**
 * The guided Gmail setup (#302): its stored progress, the console pages it
 * links to, the `gcloud` commands it shows, and which step is which.
 *
 * Most steps happen in Google's console, where netkeeper can't look, so the
 * person marks those done and `/gmail-setup` keeps what they said. The steps
 * netkeeper can check come from `/mailboxes/status` instead: whether the OAuth
 * client is saved, and whether a mailbox is connected, which also proves the
 * Gmail API is on (the callback asks Gmail for the address before it stores
 * anything, and answers `gmail_api_refused` when the API is off).
 *
 * netkeeper never runs `gcloud` and never drives a browser: every console page
 * is a plain link the person opens, and every command is text they copy.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'
import type { MailboxStatus } from '@/features/mailboxes/api'

export type GmailSetup = components['schemas']['GmailSetupOut']
export type ManualStep =
  'project' | 'gmail_api' | 'branding' | 'test_user' | 'client_created' | 'published'

export const gmailSetupKeys = { all: ['gmail-setup'] as const }

export const gmailSetupQuery = queryOptions({
  queryKey: gmailSetupKeys.all,
  queryFn: async ({ signal }): Promise<GmailSetup> => {
    const { data, error, response } = await api.GET('/api/v1/gmail-setup', { signal })
    if (data === undefined) {
      throw new Error(detailMessage(error) ?? `GET /gmail-setup returned ${response.status}`)
    }
    return data
  },
})

export async function saveGmailSetup(body: {
  project_id: string | null
  sender_email: string | null
  done: string[]
}): Promise<GmailSetup> {
  const { data, error, response } = await api.PUT('/api/v1/gmail-setup', { body })
  if (data === undefined) {
    throw new Error(detailMessage(error) ?? `PUT /gmail-setup returned ${response.status}`)
  }
  return data
}

/** Google's rule: 6 to 30 lowercase letters, digits or hyphens, a letter first, no hyphen last. */
export const PROJECT_ID_PATTERN = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/

/** A fresh project ID to suggest. Project IDs are unique across all of Google Cloud. */
export function suggestProjectId(random: () => number = Math.random): string {
  const alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
  let suffix = ''
  for (let i = 0; i < 6; i++) suffix += alphabet[Math.floor(random() * alphabet.length)]
  return `netkeeper-${suffix}`
}

const CONSOLE = 'https://console.cloud.google.com'

/** Every console page the wizard links to, for the given project. */
export function consoleLinks(projectId: string | null) {
  const project = projectId === null ? '' : `?project=${encodeURIComponent(projectId)}`
  return {
    createProject: `${CONSOLE}/projectcreate`,
    gmailApi: `${CONSOLE}/apis/library/gmail.googleapis.com${project}`,
    branding: `${CONSOLE}/auth/branding${project}`,
    audience: `${CONSOLE}/auth/audience${project}`,
    createClient: `${CONSOLE}/auth/clients/create${project}`,
    installGcloud: 'https://cloud.google.com/sdk/docs/install',
  }
}

/** The two steps `gcloud` can do, as commands to copy. netkeeper never runs them. */
export function gcloudCommands(projectId: string) {
  return {
    login: 'gcloud auth login',
    createProject: `gcloud projects create ${projectId} --name=netkeeper`,
    enableGmail: `gcloud services enable gmail.googleapis.com --project=${projectId}`,
  }
}

export type StepKey =
  | 'project'
  | 'gmail_api'
  | 'branding'
  | 'test_user'
  | 'client_created'
  | 'client'
  | 'connect'
  | 'published'

export type StepState = 'done' | 'todo' | 'failing'

export interface WizardStep {
  key: StepKey
  title: string
  state: StepState
  /** How the state is known: `checked` by netkeeper, or `marked` by you. */
  source: 'checked' | 'marked'
  optional: boolean
}

const TITLES: Record<StepKey, string> = {
  project: 'Create a Google Cloud project',
  gmail_api: 'Enable the Gmail API',
  branding: 'Set up the consent screen',
  test_user: 'Add yourself as a test user',
  client_created: 'Create a Desktop OAuth client',
  client: 'Save the client in netkeeper',
  connect: 'Connect your mailbox',
  published: 'Publish the app (optional, later)',
}

/** Reasons that say the Gmail API is off in the project. */
const API_OFF = new Set(['gmail_api_refused', 'accessNotConfigured'])

/**
 * Each step and its state. A connected mailbox proves every required step. One
 * that needs re-authorizing proves the console steps but fails the connect step.
 * A saved client proves the console steps a client can't exist without.
 */
export function wizardSteps(
  setup: Pick<GmailSetup, 'done'>,
  status: MailboxStatus,
  outcomeReason: string | undefined,
): WizardStep[] {
  const marked = new Set(setup.done)
  // A mailbox that needs re-authorizing connected once, so it proves the console
  // steps, but it isn't connected now: the connect step says so.
  const live = status.mailboxes.some((mailbox) => mailbox.status !== 'disabled')
  const connected = status.mailboxes.some((mailbox) => mailbox.status === 'ok')
  const reauth = !connected && status.mailboxes.some((m) => m.status === 'reauth_required')
  const client = status.client_configured
  const apiOff =
    !connected &&
    (API_OFF.has(outcomeReason ?? '') ||
      status.mailboxes.some((mailbox) => API_OFF.has(mailbox.status_reason ?? '')))

  const manual = (key: ManualStep, provenBy: boolean): WizardStep => ({
    key,
    title: TITLES[key],
    state: provenBy ? 'done' : marked.has(key) ? 'done' : 'todo',
    source: provenBy ? 'checked' : 'marked',
    optional: false,
  })

  const gmailApi = manual('gmail_api', live)
  if (apiOff) Object.assign(gmailApi, { state: 'failing', source: 'checked' })

  return [
    manual('project', client || live),
    gmailApi,
    manual('branding', client || live),
    manual('test_user', live),
    manual('client_created', client || live),
    {
      key: 'client',
      title: TITLES.client,
      state: client ? 'done' : 'todo',
      source: 'checked',
      optional: false,
    },
    {
      key: 'connect',
      title: TITLES.connect,
      state: connected ? 'done' : reauth ? 'failing' : 'todo',
      source: 'checked',
      optional: false,
    },
    {
      key: 'published',
      title: TITLES.published,
      state: marked.has('published') ? 'done' : 'todo',
      source: 'marked',
      optional: true,
    },
  ]
}

/** The step to show: a failing one first, then the first required step not done. */
export function currentStep(steps: WizardStep[]): StepKey {
  const failing = steps.find((step) => step.state === 'failing')
  if (failing !== undefined) return failing.key
  const next = steps.find((step) => !step.optional && step.state !== 'done')
  return next?.key ?? 'published'
}

/** Setup is complete once a mailbox is connected (`ok`); publishing stays optional. */
export function setupComplete(steps: WizardStep[]): boolean {
  return steps.every((step) => step.optional || step.state === 'done')
}
