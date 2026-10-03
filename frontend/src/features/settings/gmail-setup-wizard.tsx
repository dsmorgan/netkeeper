import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ReactNode, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { type MailboxStatus, reasonText } from '@/features/mailboxes/api'

import { ClientForm } from './client-form'
import { CopyValue } from './copy-button'
import {
  consoleLinks,
  currentStep,
  gcloudCommands,
  type GmailSetup,
  gmailSetupKeys,
  gmailSetupQuery,
  type ManualStep,
  PROJECT_ID_PATTERN,
  saveGmailSetup,
  setupComplete,
  type StepKey,
  suggestProjectId,
  type WizardStep,
  wizardSteps,
} from './gmail-setup'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const EMPTY: GmailSetup = { project_id: null, sender_email: null, done: [], steps: [] }

/**
 * A console page, opened in your own browser. netkeeper never drives it. A page
 * that needs the project ID has no `href` until you save one: it shows as
 * disabled text, with a hint, rather than a link to the wrong project.
 */
function ConsoleLink({ href, children }: { href: string | null; children: ReactNode }) {
  if (href === null) {
    return (
      <span
        role="link"
        aria-disabled="true"
        title="Save your project ID in step 1 to enable this link"
        className="font-medium text-muted-foreground line-through decoration-dotted"
      >
        {children}
      </span>
    )
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="font-medium text-primary underline underline-offset-4"
    >
      {children}
    </a>
  )
}

/** Why a link is disabled, shown once per step that has one. */
function NeedsProjectId() {
  return (
    <p role="note" className="text-muted-foreground">
      The link needs your project ID. Save it in step 1 first.
    </p>
  )
}

function stateBadge(step: WizardStep) {
  if (step.state === 'failing') return <Badge variant="destructive">problem</Badge>
  if (step.state === 'done') {
    return <Badge variant="outline">{step.source === 'checked' ? 'checked' : 'marked done'}</Badge>
  }
  return <Badge variant="secondary">{step.optional ? 'optional' : 'to do'}</Badge>
}

/**
 * Settings → Gmail's setup guide (#302): one step at a time, from no Cloud
 * project to a connected mailbox, defaulting to Testing with you as the one test
 * user. Publishing is an optional last step.
 */
export function GmailSetupWizard({
  status,
  outcomeReason,
  onConnect,
  connecting,
}: {
  status: MailboxStatus
  outcomeReason: string | undefined
  onConnect: (mailboxId: number | null) => void
  connecting: boolean
}) {
  const queryClient = useQueryClient()
  const setupQuery = useQuery(gmailSetupQuery)
  const setup = setupQuery.data ?? EMPTY
  const steps = wizardSteps(setup, status, outcomeReason)
  const complete = setupComplete(steps)
  const [open, setOpen] = useState(!complete)
  const [selected, setSelected] = useState<StepKey | null>(null)
  const active = selected ?? currentStep(steps)

  const save = useMutation({
    mutationFn: saveGmailSetup,
    onSuccess: (data) => queryClient.setQueryData(gmailSetupKeys.all, data),
  })

  const persist = (changes: Partial<Pick<GmailSetup, 'project_id' | 'sender_email' | 'done'>>) =>
    save.mutateAsync({
      project_id: changes.project_id !== undefined ? changes.project_id : setup.project_id,
      sender_email: changes.sender_email !== undefined ? changes.sender_email : setup.sender_email,
      done: changes.done ?? setup.done,
    })

  const mark = async (key: ManualStep, done: boolean) => {
    const rest = setup.done.filter((step) => step !== key)
    try {
      await persist({ done: done ? [...rest, key] : rest })
    } catch {
      return // the alert above says why
    }
    // Move on to whatever is next, not the step just marked.
    setSelected(null)
  }

  const required = steps.filter((step) => !step.optional)
  const finished = required.filter((step) => step.state === 'done').length

  if (setupQuery.isPending) return <p role="status">Loading the setup guide…</p>

  if (complete && !open) {
    return (
      <section aria-label="Setup guide" className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground">Gmail setup is complete.</span>
        <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
          Show setup steps
        </Button>
      </section>
    )
  }

  return (
    <section aria-label="Setup guide" className="space-y-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">Setup guide</h3>
        <span className="text-xs text-muted-foreground">
          {finished} of {required.length} steps done
        </span>
      </div>
      <p className="text-muted-foreground">
        Each step opens the right page of Google’s console in your browser. netkeeper checks what it
        can (the client, the token, the Gmail API); you mark the rest done. Your app stays in
        Testing, with you as its one test user.
      </p>
      {setupQuery.isError && (
        <p role="alert" className="text-destructive">
          Your setup progress didn’t load: {message(setupQuery.error)}
        </p>
      )}
      {save.isError && (
        <p role="alert" className="text-destructive">
          {message(save.error)}
        </p>
      )}

      <ol className="space-y-1">
        {steps.map((step, index) => {
          const isActive = step.key === active
          return (
            <li key={step.key} className="rounded-md border">
              <button
                type="button"
                className="flex w-full items-center gap-2 px-3 py-2 text-left"
                aria-expanded={isActive}
                aria-controls={`gmail-step-${step.key}`}
                onClick={() => setSelected(step.key)}
              >
                <span className="w-5 text-muted-foreground">{index + 1}.</span>
                <span className={isActive ? 'font-medium' : undefined}>{step.title}</span>
                <span className="ml-auto">{stateBadge(step)}</span>
              </button>
              {isActive && (
                <div id={`gmail-step-${step.key}`} className="space-y-3 border-t px-3 py-3">
                  <StepBody
                    step={step}
                    setup={setup}
                    status={status}
                    saving={save.isPending}
                    persist={persist}
                    mark={mark}
                    onConnect={onConnect}
                    connecting={connecting}
                    moveOn={() => setSelected(null)}
                  />
                </div>
              )}
            </li>
          )
        })}
      </ol>
      {complete && (
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
          Hide setup steps
        </Button>
      )}
    </section>
  )
}

function MarkDone({
  step,
  label,
  mark,
  saving,
  disabled = false,
}: {
  step: WizardStep
  label: string
  mark: (key: ManualStep, done: boolean) => Promise<void>
  saving: boolean
  disabled?: boolean
}) {
  const key = step.key as ManualStep
  if (step.state === 'done' && step.source === 'checked') return null
  if (step.state === 'done') {
    return (
      <Button variant="ghost" size="sm" disabled={saving} onClick={() => void mark(key, false)}>
        Mark not done
      </Button>
    )
  }
  return (
    <Button size="sm" disabled={saving || disabled} onClick={() => void mark(key, true)}>
      {label}
    </Button>
  )
}

function StepBody({
  step,
  setup,
  status,
  saving,
  persist,
  mark,
  onConnect,
  connecting,
  moveOn,
}: {
  step: WizardStep
  setup: GmailSetup
  status: MailboxStatus
  saving: boolean
  persist: (
    changes: Partial<Pick<GmailSetup, 'project_id' | 'sender_email' | 'done'>>,
  ) => Promise<GmailSetup>
  mark: (key: ManualStep, done: boolean) => Promise<void>
  onConnect: (mailboxId: number | null) => void
  connecting: boolean
  moveOn: () => void
}) {
  const links = consoleLinks(setup.project_id)
  const sender = setup.sender_email ?? 'the Gmail address you send from'
  const commands = setup.project_id === null ? null : gcloudCommands(setup.project_id)
  const hint = setup.project_id === null ? <NeedsProjectId /> : null

  switch (step.key) {
    case 'project':
      return (
        <>
          <ProjectForm setup={setup} saving={saving} persist={persist} />
          <p className="text-muted-foreground">
            Already have a project? Use its <strong>ID</strong>, not its name. In the console,
            choose the project picker at the top; the list shows <strong>Name</strong>,{' '}
            <strong>Type</strong> and <strong>ID</strong>. Google often adds a number to the ID, so{' '}
            <code className="font-mono">netkeeper</code> might be{' '}
            <code className="font-mono">netkeeper-510123</code>. Every link below uses the ID, and
            a wrong one lands on a confusing permission page with no error.
          </p>
          {setup.project_id !== null && commands !== null && (
            <>
              <p>
                Open <ConsoleLink href={links.createProject}>New project</ConsoleLink>. Name it{' '}
                <code className="font-mono">netkeeper</code>, then choose <strong>Edit</strong>{' '}
                under the project ID and enter this one. Any Google account can own the project.
              </p>
              <CopyValue label="Project ID" value={setup.project_id} />
              <details>
                <summary className="cursor-pointer text-muted-foreground">
                  Or use gcloud, if you have it
                </summary>
                <div className="mt-2 space-y-2">
                  <p className="text-muted-foreground">
                    Run these yourself in a terminal; netkeeper never runs them.{' '}
                    <ConsoleLink href={links.installGcloud}>Install gcloud</ConsoleLink>.
                  </p>
                  <CopyValue label="Log in" value={commands.login} />
                  <CopyValue label="Create the project" value={commands.createProject} />
                </div>
              </details>
            </>
          )}
          <MarkDone
            step={step}
            label="Done: the project exists"
            mark={mark}
            saving={saving}
            disabled={setup.project_id === null}
          />
        </>
      )
    case 'gmail_api':
      return (
        <>
          {step.state === 'failing' && (
            <p role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-destructive">
              Gmail refused the token when you connected: the Gmail API is off in this project.
              Enable it, then connect again.
            </p>
          )}
          <p>
            Open the <ConsoleLink href={links.gmailApi}>Gmail API</ConsoleLink> page for your
            project and choose <strong>Enable</strong>.
          </p>
          {hint}
          {commands !== null && (
            <details>
              <summary className="cursor-pointer text-muted-foreground">Or use gcloud</summary>
              <div className="mt-2">
                <CopyValue label="Enable the Gmail API" value={commands.enableGmail} />
              </div>
            </details>
          )}
          <p className="text-muted-foreground">
            netkeeper checks this when you connect your mailbox: if Gmail refuses, this step comes
            back marked as the problem.
          </p>
          <MarkDone step={step} label="Done: it’s enabled" mark={mark} saving={saving} />
        </>
      )
    case 'branding':
      return (
        <>
          <p>
            Open <ConsoleLink href={links.branding}>Branding</ConsoleLink> (choose{' '}
            <strong>Get started</strong> if the console offers it) and fill in:
          </p>
          {hint}
          <div className="space-y-1">
            <CopyValue label="App name" value="netkeeper" />
            <CopyValue label="User support email" value={sender} />
            <p>
              <span className="text-muted-foreground">Audience:</span> <strong>External</strong>{' '}
              <span className="text-muted-foreground">
                (Internal is only for Google Workspace organizations)
              </span>
            </p>
            <CopyValue label="Contact email" value={sender} />
          </div>
          <p className="text-muted-foreground">
            Leave the homepage and privacy policy links empty. You only need them to publish, the
            optional last step.
          </p>
          <MarkDone
            step={step}
            label="Done: the consent screen exists"
            mark={mark}
            saving={saving}
          />
        </>
      )
    case 'test_user':
      return (
        <>
          <p>
            Your app starts in <strong>Testing</strong>: only the test users you list can authorize
            it. Open <ConsoleLink href={links.audience}>Audience</ConsoleLink>, and under{' '}
            <strong>Test users</strong> choose <strong>Add users</strong>:
          </p>
          {hint}
          <CopyValue label="Test user" value={sender} />
          <p className="text-muted-foreground">
            In Testing, Google expires the token after 7 days. netkeeper notices, pauses email
            steps, and shows a banner; you choose Re-authorize. Publishing (the last step) removes
            the limit.
          </p>
          <MarkDone step={step} label="Done: I’m a test user" mark={mark} saving={saving} />
        </>
      )
    case 'client_created':
      return (
        <>
          <p>
            Open <ConsoleLink href={links.createClient}>Create OAuth client</ConsoleLink>:
          </p>
          {hint}
          <div className="space-y-1">
            <p>
              <span className="text-muted-foreground">Application type:</span>{' '}
              <strong>Desktop app</strong>{' '}
              <span className="text-muted-foreground">
                (not Web application: netkeeper refuses a web client)
              </span>
            </p>
            <CopyValue label="Name" value="netkeeper" />
          </div>
          <p className="text-muted-foreground">
            Choose <strong>Create</strong>. Keep the dialog open, or download the JSON: the next
            step needs the client ID and secret.
          </p>
          <MarkDone step={step} label="Done: the client exists" mark={mark} saving={saving} />
        </>
      )
    case 'client':
      return status.client_configured ? (
        <p>
          Saved: <code className="font-mono text-xs break-all">{status.client_id}</code>
        </p>
      ) : (
        <>
          <p>Paste the client ID and secret Google showed you.</p>
          <ClientForm onSaved={moveOn} />
        </>
      )
    case 'connect': {
      const connected = status.mailboxes.find((mailbox) => mailbox.status === 'ok')
      if (connected !== undefined) {
        return (
          <p>
            Connected: <strong>{connected.email}</strong>. netkeeper checked that the token works
            and that Gmail answers it.
          </p>
        )
      }
      const stale = status.mailboxes.find((mailbox) => mailbox.status === 'reauth_required')
      if (stale !== undefined) {
        return (
          <>
            <p role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-destructive">
              <strong>{stale.email}</strong> needs re-authorizing.{' '}
              {reasonText(stale.status_reason) ?? 'Google no longer accepts its token.'}
            </p>
            <Button size="sm" onClick={() => onConnect(stale.id)} disabled={connecting}>
              {connecting ? 'Opening Google…' : 'Re-authorize'}
            </Button>
          </>
        )
      }
      return (
        <>
          <p>
            Authorize the Gmail account on Google’s page. Google asks for read, compose, send and
            label access (<code className="font-mono">gmail.modify</code>); netkeeper never deletes
            mail.
          </p>
          <p>
            Google’s page says <strong>Google hasn’t verified this app</strong>. That’s expected:
            the app is yours. Choose <strong>Advanced</strong>, then{' '}
            <strong>Go to netkeeper (unsafe)</strong>, and leave the Gmail box ticked.
          </p>
          {!status.client_configured && (
            <p className="text-muted-foreground">Save the OAuth client first.</p>
          )}
          <Button
            size="sm"
            onClick={() => onConnect(null)}
            disabled={!status.client_configured || connecting}
          >
            {connecting ? 'Opening Google…' : 'Connect Gmail'}
          </Button>
        </>
      )
    }
    case 'published':
      return (
        <>
          <p>
            You don’t need to publish. Publishing only stops the token expiring every 7 days. An
            unverified app that only you use still shows the same warning screen.
          </p>
          <p>
            Google enables <strong>Publish app</strong> (under{' '}
            <ConsoleLink href={links.audience}>Audience</ConsoleLink>) only once{' '}
            <ConsoleLink href={links.branding}>Branding</ConsoleLink> has an app name, a support
            email, an <strong>app home page</strong> and a <strong>privacy policy link</strong>.
            Google’s rules say both pages must be on a domain you own, listed under{' '}
            <strong>Authorized domains</strong>, with the privacy policy on the home page’s domain.
          </p>
          {hint}
          <p className="text-muted-foreground">
            A GitHub repository URL (github.com) isn’t a domain you own, so Google’s rules don’t
            allow it. netkeeper hasn’t confirmed whether the console refuses it for an app that’s
            never submitted for verification. A GitHub Pages site (you.github.io) probably fits:
            github.io is a public suffix, so you should be able to verify you.github.io in Google
            Search Console, but netkeeper hasn’t confirmed that either. If you have neither, stay in
            Testing.
          </p>
          <MarkDone step={step} label="Done: it’s published" mark={mark} saving={saving} />
        </>
      )
  }
}

function ProjectForm({
  setup,
  saving,
  persist,
}: {
  setup: GmailSetup
  saving: boolean
  persist: (
    changes: Partial<Pick<GmailSetup, 'project_id' | 'sender_email' | 'done'>>,
  ) => Promise<GmailSetup>
}) {
  const [projectId, setProjectId] = useState(setup.project_id ?? '')
  const [sender, setSender] = useState(setup.sender_email ?? '')
  const trimmed = projectId.trim().toLowerCase()
  const valid = PROJECT_ID_PATTERN.test(trimmed)
  const unchanged =
    trimmed === (setup.project_id ?? '') && sender.trim() === (setup.sender_email ?? '')

  return (
    <form
      className="grid gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        void persist({ project_id: trimmed, sender_email: sender.trim() || null }).catch(() => {})
      }}
    >
      <Label htmlFor="gmail-setup-project">Project ID</Label>
      <div className="flex gap-2">
        <Input
          id="gmail-setup-project"
          value={projectId}
          onChange={(event) => setProjectId(event.target.value)}
          placeholder="netkeeper-ab12cd"
          autoComplete="off"
          aria-invalid={projectId !== '' && !valid}
          required
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setProjectId(suggestProjectId())}
        >
          Suggest one
        </Button>
      </div>
      {projectId !== '' && !valid && (
        <p className="text-xs text-destructive">
          6 to 30 lowercase letters, digits or hyphens, starting with a letter and not ending with a
          hyphen.
        </p>
      )}
      <Label htmlFor="gmail-setup-sender">Gmail address you send from</Label>
      <Input
        id="gmail-setup-sender"
        type="email"
        value={sender}
        onChange={(event) => setSender(event.target.value)}
        placeholder="you@gmail.com"
        autoComplete="email"
      />
      <div>
        <Button type="submit" size="sm" disabled={!valid || saving || unchanged}>
          {saving ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </form>
  )
}
