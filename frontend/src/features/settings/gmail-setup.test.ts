import { describe, expect, it } from 'vitest'

import { mailbox, status } from '@/features/mailboxes/test-support'

import {
  consoleLinks,
  currentStep,
  gcloudCommands,
  PROJECT_ID_PATTERN,
  setupComplete,
  suggestProjectId,
  wizardSteps,
} from './gmail-setup'

const nothing = status({ client_configured: false, client_id: null })

function states(steps: ReturnType<typeof wizardSteps>) {
  return Object.fromEntries(steps.map((step) => [step.key, `${step.state}/${step.source}`]))
}

describe('consoleLinks', () => {
  it('names the project on every page that needs it', () => {
    expect(consoleLinks('netkeeper-ab12cd')).toEqual({
      createProject: 'https://console.cloud.google.com/projectcreate',
      gmailApi:
        'https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=netkeeper-ab12cd',
      branding: 'https://console.cloud.google.com/auth/branding?project=netkeeper-ab12cd',
      audience: 'https://console.cloud.google.com/auth/audience?project=netkeeper-ab12cd',
      createClient: 'https://console.cloud.google.com/auth/clients/create?project=netkeeper-ab12cd',
      installGcloud: 'https://cloud.google.com/sdk/docs/install',
    })
  })

  it('leaves the project off before there is one', () => {
    expect(consoleLinks(null).branding).toBe('https://console.cloud.google.com/auth/branding')
  })
})

describe('gcloudCommands', () => {
  it('spells out the two steps gcloud can do', () => {
    expect(gcloudCommands('netkeeper-ab12cd')).toEqual({
      login: 'gcloud auth login',
      createProject: 'gcloud projects create netkeeper-ab12cd --name=netkeeper',
      enableGmail: 'gcloud services enable gmail.googleapis.com --project=netkeeper-ab12cd',
    })
  })
})

describe('project IDs', () => {
  it('suggests one Google accepts', () => {
    let n = 0
    const id = suggestProjectId(() => (n++ % 36) / 36)
    expect(id).toBe('netkeeper-abcdef')
    for (let i = 0; i < 50; i++) expect(suggestProjectId()).toMatch(PROJECT_ID_PATTERN)
  })

  it.each(['short', '1netkeeper', 'netkeeper-', 'Netkeeper-x1', 'net keeper', 'a'.repeat(31)])(
    'refuses %s',
    (id) => expect(PROJECT_ID_PATTERN.test(id)).toBe(false),
  )
})

describe('wizardSteps', () => {
  it('starts at the project with nothing done', () => {
    const steps = wizardSteps({ done: [] }, nothing, undefined)
    expect(steps.map((step) => step.key)).toEqual([
      'project',
      'gmail_api',
      'branding',
      'test_user',
      'client_created',
      'client',
      'connect',
      'published',
    ])
    expect(steps.every((step) => step.state === 'todo')).toBe(true)
    expect(currentStep(steps)).toBe('project')
    expect(setupComplete(steps)).toBe(false)
  })

  it('takes marked steps as done and moves to the next', () => {
    const steps = wizardSteps({ done: ['project', 'gmail_api'] }, nothing, undefined)
    expect(currentStep(steps)).toBe('branding')
    expect(steps[0]).toMatchObject({ state: 'done', source: 'marked' })
  })

  it('counts a saved client as proof of the console steps it needs', () => {
    const steps = wizardSteps({ done: [] }, status(), undefined)
    expect(states(steps)).toMatchObject({
      project: 'done/checked',
      gmail_api: 'todo/marked',
      branding: 'done/checked',
      test_user: 'todo/marked',
      client_created: 'done/checked',
      client: 'done/checked',
      connect: 'todo/checked',
    })
    expect(currentStep(steps)).toBe('gmail_api')
  })

  it('is complete once a mailbox is connected, publishing or not', () => {
    const steps = wizardSteps({ done: [] }, status({ mailboxes: [mailbox()] }), undefined)
    expect(setupComplete(steps)).toBe(true)
    expect(steps.find((step) => step.key === 'published')?.state).toBe('todo')
    expect(currentStep(steps)).toBe('published')
  })

  it('a disconnected mailbox proves nothing', () => {
    const gone = mailbox({ status: 'disabled', status_reason: 'disconnected' })
    const steps = wizardSteps({ done: [] }, status({ mailboxes: [gone] }), undefined)
    expect(setupComplete(steps)).toBe(false)
    expect(currentStep(steps)).toBe('gmail_api')
  })

  it('sends you back to the Gmail API when Gmail refused the token', () => {
    const marked = ['project', 'gmail_api', 'branding', 'test_user', 'client_created']
    const steps = wizardSteps({ done: marked }, status(), 'gmail_api_refused')
    expect(steps[1]).toMatchObject({ key: 'gmail_api', state: 'failing', source: 'checked' })
    expect(currentStep(steps)).toBe('gmail_api')
    const other = wizardSteps({ done: marked }, status(), 'access_denied')
    expect(currentStep(other)).toBe('connect')
  })
})
