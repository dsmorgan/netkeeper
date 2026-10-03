import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type MailboxStatus, navigation } from '@/features/mailboxes/api'
import { emptySetup, mailbox, renderWithBackend, status } from '@/features/mailboxes/test-support'
import { resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse } from '@/test/fetch'

import { GmailSection, type GmailOutcome } from './gmail-section'

const writeText = vi.fn<(text: string) => Promise<void>>()

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
})

afterEach(() => {
  resetFakeEventSource()
  vi.restoreAllMocks()
})

const PROJECT = 'netkeeper-ab12cd'

/** A fake `/gmail-setup` that keeps what the wizard PUTs, as the backend does. */
function render(
  current: () => MailboxStatus,
  initial: Record<string, unknown> = {},
  outcome: GmailOutcome = {},
) {
  let setup = emptySetup(initial)
  const rendered = renderWithBackend(<GmailSection outcome={outcome} />, current, {
    'GET /api/v1/gmail-setup': () => jsonResponse(setup),
    'PUT /api/v1/gmail-setup': (body) => {
      setup = emptySetup(body as Record<string, unknown>)
      return jsonResponse(setup)
    },
  })
  return { ...rendered, setup: () => setup }
}

const noClient = () => status({ client_configured: false, client_id: null })

function step(name: RegExp): HTMLElement {
  return screen.getByRole('button', { name }).closest('li') as HTMLElement
}

function link(name: string): HTMLAnchorElement {
  return screen.getByRole('link', { name }) as HTMLAnchorElement
}

describe('Gmail setup guide', () => {
  it('starts at step 1 and saves the project ID and sending address', async () => {
    const { calls } = render(noClient)
    const first = await screen.findByRole('button', { name: /Create a Google Cloud project/ })
    expect(first).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('0 of 7 steps done')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Done: the project exists' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Project ID'), { target: { value: ` ${PROJECT} ` } })
    fireEvent.change(screen.getByLabelText('Gmail address you send from'), {
      target: { value: 'me@example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: 'PUT',
        path: '/api/v1/gmail-setup',
        body: { project_id: PROJECT, sender_email: 'me@example.com', done: [] },
      }),
    )
    expect(await screen.findByRole('link', { name: 'New project' })).toHaveAttribute(
      'href',
      'https://console.cloud.google.com/projectcreate',
    )
    expect(
      screen.getByText('gcloud projects create netkeeper-ab12cd --name=netkeeper'),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Done: the project exists' })).toBeEnabled()
  })

  it('explains where to find the project ID', async () => {
    render(noClient)
    await screen.findByLabelText('Project ID')
    const help = screen.getByText(/project picker/)
    expect(help).toHaveTextContent(/Name, Type and ID/)
    expect(help).toHaveTextContent(/netkeeper-510123/)
  })

  it('disables every project link, with a hint, until a project ID is saved', async () => {
    const marked = ['project', 'gmail_api', 'branding', 'test_user', 'client_created']
    const { setup } = render(noClient, { done: marked })
    for (const [stepName, linkName] of [
      [/Enable the Gmail API/, 'Gmail API'],
      [/Set up the consent screen/, 'Branding'],
      [/Add yourself as a test user/, 'Audience'],
      [/Create a Desktop OAuth client/, 'Create OAuth client'],
      [/Publish the app/, 'Audience'],
      [/Publish the app/, 'Branding'],
    ] as const) {
      fireEvent.click(await screen.findByRole('button', { name: stepName }))
      const disabled = screen.getByRole('link', { name: linkName })
      expect(disabled).toHaveAttribute('aria-disabled', 'true')
      expect(disabled).not.toHaveAttribute('href')
      expect(screen.getByRole('note')).toHaveTextContent(/needs your project ID/)
    }
    expect(setup().project_id).toBeNull()
  })

  it('uses the project ID in every deep link, never the name', async () => {
    const marked = ['project', 'gmail_api', 'branding', 'test_user', 'client_created']
    render(noClient, { project_id: 'netkeeper-510123', done: marked })
    const hrefs: string[] = []
    for (const [stepName, linkName] of [
      [/Enable the Gmail API/, 'Gmail API'],
      [/Set up the consent screen/, 'Branding'],
      [/Add yourself as a test user/, 'Audience'],
      [/Create a Desktop OAuth client/, 'Create OAuth client'],
      [/Publish the app/, 'Audience'],
      [/Publish the app/, 'Branding'],
    ] as const) {
      fireEvent.click(await screen.findByRole('button', { name: stepName }))
      hrefs.push(link(linkName).href)
    }
    expect(hrefs).toHaveLength(6)
    for (const href of hrefs) {
      expect(new URL(href).searchParams.get('project')).toBe('netkeeper-510123')
    }
  })

  it('refuses a project ID Google would refuse', async () => {
    render(noClient)
    fireEvent.change(await screen.findByLabelText('Project ID'), { target: { value: '1bad' } })
    expect(
      screen.getByText(/starting with a letter and not ending with a hyphen/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('suggests a project ID', async () => {
    render(noClient)
    fireEvent.click(await screen.findByRole('button', { name: 'Suggest one' }))
    expect((screen.getByLabelText('Project ID') as HTMLInputElement).value).toMatch(
      /^netkeeper-[a-z0-9]{6}$/,
    )
  })

  it('marks a console step done and moves on to the next', async () => {
    const { calls, setup } = render(noClient, { project_id: PROJECT })
    fireEvent.click(await screen.findByRole('button', { name: 'Done: the project exists' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Enable the Gmail API/ })).toHaveAttribute(
        'aria-expanded',
        'true',
      ),
    )
    expect(calls).toContainEqual({
      method: 'PUT',
      path: '/api/v1/gmail-setup',
      body: { project_id: PROJECT, sender_email: null, done: ['project'] },
    })
    expect(setup().done).toEqual(['project'])
    expect(
      within(step(/Create a Google Cloud project/)).getByText('marked done'),
    ).toBeInTheDocument()
    expect(link('Gmail API')).toHaveAttribute(
      'href',
      `https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=${PROJECT}`,
    )
    expect(link('Gmail API')).toHaveAttribute('target', '_blank')
    expect(link('Gmail API')).toHaveAttribute('rel', 'noopener noreferrer')
    expect(
      screen.getByText(`gcloud services enable gmail.googleapis.com --project=${PROJECT}`),
    ).toBeInTheDocument()
  })

  it('undoes a step marked done by mistake', async () => {
    const { setup } = render(noClient, { project_id: PROJECT, done: ['project'] })
    fireEvent.click(await screen.findByRole('button', { name: /Create a Google Cloud project/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Mark not done' }))
    await waitFor(() => expect(setup().done).toEqual([]))
  })

  it('gives the consent screen’s values to copy', async () => {
    render(noClient, {
      project_id: PROJECT,
      sender_email: 'me@example.com',
      done: ['project', 'gmail_api'],
    })
    expect(await screen.findByRole('link', { name: 'Branding' })).toHaveAttribute(
      'href',
      `https://console.cloud.google.com/auth/branding?project=${PROJECT}`,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Copy app name' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('netkeeper'))
    expect(await screen.findByRole('button', { name: 'Copy app name' })).toHaveTextContent('Copied')
    fireEvent.click(screen.getByRole('button', { name: 'Copy user support email' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('me@example.com'))
    expect(
      screen.getByText(/Leave the homepage and privacy policy links empty/),
    ).toBeInTheDocument()
  })

  it('says so when the clipboard refuses', async () => {
    writeText.mockRejectedValue(new Error('denied'))
    render(noClient, { project_id: PROJECT, done: ['project', 'gmail_api'] })
    fireEvent.click(await screen.findByRole('button', { name: 'Copy app name' }))
    expect(await screen.findByText('Select it by hand')).toBeInTheDocument()
  })

  it('makes adding yourself as a test user its own step, in Testing', async () => {
    render(noClient, {
      project_id: PROJECT,
      sender_email: 'me@example.com',
      done: ['project', 'gmail_api', 'branding'],
    })
    const body = await screen.findByText(/Your app starts in/)
    expect(body).toHaveTextContent('only the test users you list can authorize it')
    expect(link('Audience')).toHaveAttribute(
      'href',
      `https://console.cloud.google.com/auth/audience?project=${PROJECT}`,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Copy test user' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('me@example.com'))
    expect(screen.getByText(/Google expires the token after 7 days/)).toBeInTheDocument()
  })

  it('creates a Desktop client, not a web one', async () => {
    render(noClient, {
      project_id: PROJECT,
      done: ['project', 'gmail_api', 'branding', 'test_user'],
    })
    expect(await screen.findByRole('link', { name: 'Create OAuth client' })).toHaveAttribute(
      'href',
      `https://console.cloud.google.com/auth/clients/create?project=${PROJECT}`,
    )
    expect(screen.getByText('Desktop app')).toBeInTheDocument()
    expect(screen.getByText(/not Web application/)).toBeInTheDocument()
  })

  it('checks the client itself once it is saved, then moves on to connecting', async () => {
    let current = noClient()
    const marked = ['project', 'gmail_api', 'branding', 'test_user', 'client_created']
    renderWithBackend(<GmailSection outcome={{}} />, () => current, {
      'GET /api/v1/gmail-setup': () =>
        jsonResponse(emptySetup({ project_id: PROJECT, done: marked })),
      'PUT /api/v1/mailboxes/oauth/client': () => {
        current = status()
        return jsonResponse(current)
      },
    })
    const saveStep = await screen.findByRole('button', { name: /Save the client in netkeeper/ })
    expect(saveStep).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('5 of 7 steps done')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Client ID'), {
      target: { value: 'abc.apps.googleusercontent.com' },
    })
    fireEvent.change(screen.getByLabelText('Client secret'), { target: { value: 's3cret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save client' }))

    expect(await screen.findByText('6 of 7 steps done')).toBeInTheDocument()
    expect(within(step(/Save the client in netkeeper/)).getByText('checked')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Connect your mailbox/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    expect(screen.getByRole('button', { name: 'Connect Gmail' })).toBeEnabled()
  })

  it('points back at the Gmail API when Gmail refused the token', async () => {
    render(
      () => status(),
      { project_id: PROJECT, done: ['project', 'gmail_api', 'branding', 'test_user'] },
      { gmail: 'error', reason: 'gmail_api_refused' },
    )
    const api = await screen.findByRole('button', { name: /Enable the Gmail API/ })
    expect(api).toHaveAttribute('aria-expanded', 'true')
    expect(within(api).getByText('problem')).toBeInTheDocument()
    expect(screen.getByText(/the Gmail API is off in this project/)).toBeInTheDocument()
  })

  it('folds away once a mailbox is connected, keeping publishing for later', async () => {
    render(() => status({ mailboxes: [mailbox()] }))
    expect(await screen.findByText('Gmail setup is complete.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Publish the app/ })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Show setup steps' }))
    expect(screen.getByText('7 of 7 steps done')).toBeInTheDocument()
    const publish = screen.getByRole('button', { name: /Publish the app/ })
    expect(publish).toHaveAttribute('aria-expanded', 'true')
    expect(within(publish).getByText('optional')).toBeInTheDocument()
    expect(screen.getByText(/app home page/)).toBeInTheDocument()
    expect(
      screen.getByText(/A GitHub repository URL \(github.com\) isn’t a domain you own/),
    ).toBeInTheDocument()
    expect(screen.getByText(/A GitHub repository URL/)).toHaveTextContent(
      'netkeeper hasn’t confirmed that either',
    )
    expect(within(step(/Connect your mailbox/)).getByText('checked')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Hide setup steps' }))
    expect(screen.getByText('Gmail setup is complete.')).toBeInTheDocument()
  })

  it('shows a mailbox that needs re-authorizing as not connected, and re-authorizes it', async () => {
    vi.spyOn(navigation, 'assign').mockImplementation(() => {})
    const stale = mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })
    const { calls } = renderWithBackend(
      <GmailSection outcome={{}} />,
      () => status({ mailboxes: [stale] }),
      {
        'POST /api/v1/mailboxes/oauth/start': () =>
          jsonResponse({ authorization_url: 'https://accounts.example/auth' }),
      },
    )
    const connect = await screen.findByRole('button', { name: /Connect your mailbox/ })
    expect(connect).toHaveAttribute('aria-expanded', 'true')
    expect(within(connect).getByText('problem')).toBeInTheDocument()
    expect(screen.queryByText('Gmail setup is complete.')).not.toBeInTheDocument()
    expect(screen.queryByText(/checked that the token works/)).not.toBeInTheDocument()
    const body = document.getElementById('gmail-step-connect') as HTMLElement
    expect(within(body).getByRole('alert')).toHaveTextContent(
      'sender@example.com needs re-authorizing',
    )
    fireEvent.click(within(body).getByRole('button', { name: 'Re-authorize' }))
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: 'POST',
        path: '/api/v1/mailboxes/oauth/start',
        body: { mailbox_id: 3 },
      }),
    )
  })

  it('explains the scope before connecting', async () => {
    render(() => status(), {
      project_id: PROJECT,
      done: ['project', 'gmail_api', 'branding', 'test_user', 'client_created'],
    })
    const body = await screen.findByText(/Authorize the Gmail account on Google’s page/)
    expect(body).toHaveTextContent('gmail.modify')
    expect(body).toHaveTextContent('netkeeper never deletes mail')
  })

  it('opens any step on request', async () => {
    render(noClient)
    fireEvent.click(await screen.findByRole('button', { name: /Add yourself as a test user/ }))
    expect(screen.getByRole('button', { name: /Add yourself as a test user/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    expect(screen.getByRole('button', { name: /Create a Google Cloud project/ })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })

  it('shows why progress was not saved', async () => {
    renderWithBackend(<GmailSection outcome={{}} />, noClient, {
      'GET /api/v1/gmail-setup': () => jsonResponse(emptySetup({ project_id: PROJECT })),
      'PUT /api/v1/gmail-setup': () => jsonResponse({ detail: 'unknown setup steps: x' }, 422),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Done: the project exists' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('unknown setup steps: x')
  })
})
