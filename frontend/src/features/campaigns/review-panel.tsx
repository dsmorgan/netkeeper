/**
 * The review gate on one screen (spec 11.8, P3-09's API).
 *
 * The checklist at the top is the API's own `missing` list, so it never claims
 * a requirement is met that the gate would refuse. Each section below records
 * one requirement: each step approved once, after paging through its rendered
 * messages (a step whose template uses `{{ personal_line }}` has each message
 * approved on its own instead), lint, a test per email step to your own
 * mailbox (a draft in your Drafts while Gmail is armed for drafts, a message sent
 * to you once it is armed to send). The guard summary is one line of who will start
 * and who is skipped, with each skipped contact on demand; it gates nothing, since the
 * guards apply again when each step fires (#346). Activation asks first, with the
 * scheduled start (#338), and a `409` shows what is still missing.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronLeft, ChevronRight, CircleDashed } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Callout, ErrorNote, LoadingNote } from '@/features/crm/controls'

import { UNCHOSEN, startIso, type StartChoice } from './start'
import { StartPicker } from './start-picker'

import {
  CampaignApiError,
  STEP_PAGE,
  activateCampaign,
  approveMessages,
  approveStep,
  campaignKeys,
  errorText,
  guardDetailsQuery,
  lintCampaign,
  reviewQuery,
  startReview,
  stepReviewQuery,
  testSend,
  type Campaign,
  type LintResult,
  type MessagePreview,
  type Missing,
  type Step,
  type TestSend,
} from './api'
import { REQUIREMENTS, REQUIREMENT_LABELS, formatWhen, missingText } from './format'

export function ReviewPanel({ campaign }: { campaign: Campaign }) {
  const id = campaign.id
  const queryClient = useQueryClient()
  const review = useQuery(reviewQuery(id))
  const reviewing = campaign.status === 'reviewing'

  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: campaignKeys.review(id) }),
      queryClient.invalidateQueries({ queryKey: campaignKeys.steps(id) }),
      queryClient.invalidateQueries({ queryKey: campaignKeys.one(id) }),
      queryClient.invalidateQueries({ queryKey: campaignKeys.list() }),
    ])

  const start = useMutation({ mutationFn: () => startReview(id), onSuccess: refresh })

  if (review.isPending) return <LoadingNote label="Loading the review…" />
  if (review.isError) return <ErrorNote label="The review is unavailable." error={review.error} />
  const missing = review.data.missing

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Review</CardTitle>
        <CardDescription>
          Activation needs every item below. The list is the gate&apos;s own: an edit to a step, a
          template, a contact or the audience undoes what it touched.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <Checklist missing={missing} />
        {!reviewing ? (
          <div className="flex flex-col gap-2">
            <p className="text-sm text-muted-foreground">
              Start the review once the audience is enrolled. The audience source is fixed from then
              on.
            </p>
            <Button className="w-fit" onClick={() => start.mutate()} disabled={start.isPending}>
              Start review
            </Button>
            {start.isError && <ErrorNote label="The review did not start." error={start.error} />}
          </div>
        ) : (
          <>
            <StepsSection campaign={campaign} onChange={refresh} />
            <LintSection campaignId={id} missing={missing} onChange={refresh} />
            <TestSendSection campaign={campaign} missing={missing} onChange={refresh} />
            <GuardsSection
              campaignId={id}
              summary={review.data.guard_summary}
              priorContact={review.data.prior_contact_note ?? null}
            />
            <ActivateSection campaign={campaign} missing={missing} onChange={refresh} />
          </>
        )}
      </CardContent>
    </Card>
  )
}

function Checklist({ missing }: { missing: Missing[] }) {
  return (
    <ul aria-label="Review checklist" className="flex flex-col gap-1 text-sm">
      {REQUIREMENTS.map((requirement) => {
        const gaps = missing.filter((m) => m.requirement === requirement)
        const met = gaps.length === 0
        return (
          <li key={requirement} className="flex items-start gap-2">
            {met ? (
              <Check className="mt-0.5 size-4 shrink-0 text-emerald-600" aria-hidden="true" />
            ) : (
              <CircleDashed
                className="mt-0.5 size-4 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />
            )}
            <span>
              <span className="font-medium">{REQUIREMENT_LABELS[requirement]}</span>
              {met ? (
                <span className="text-muted-foreground">: done</span>
              ) : (
                <span className="text-muted-foreground">
                  {': missing, '}
                  {gaps.map(missingText).join('; ')}
                </span>
              )}
            </span>
          </li>
        )
      })}
    </ul>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2 border-t pt-4">
      <h3 className="font-medium">{title}</h3>
      {children}
    </section>
  )
}

function isMissing(missing: Missing[], requirement: string) {
  return missing.some((m) => m.requirement === requirement)
}

/**
 * Whether a refusal says the thing shown is no longer current (a stale fingerprint).
 * Only the server's `code: stale` counts: any other 409 (the campaign no longer under
 * review, an enrollment no longer pending) is a real refusal, shown as an error.
 */
function isStale(error: unknown): boolean {
  return error instanceof CampaignApiError && error.status === 409 && error.code === 'stale'
}

const STALE_STEP =
  'Something changed since this step was shown (its template, its settings or the contact), ' +
  'so it was refreshed. Read it again before approving.'

function StaleNotice({ notice }: { notice: string | null }) {
  if (notice === null) return null
  return (
    <p role="status" className="rounded-lg bg-amber-500/10 px-3 py-2 text-sm">
      {notice}
    </p>
  )
}

function StepsSection({
  campaign,
  onChange,
}: {
  campaign: Campaign
  onChange: () => Promise<unknown>
}) {
  return (
    <Section title="Steps">
      <p className="text-sm text-muted-foreground">
        Page through each step&apos;s messages, rendered for every enrolled contact, then approve
        the step once. The approval covers the step&apos;s messages, including any rendered later,
        until you edit the step or its template. A message that can&apos;t be sent is listed apart
        and stays blocked.
      </p>
      <ol className="flex flex-col gap-3">
        {campaign.steps.map((step) => (
          <li key={step.id}>
            <StepReviewCard campaignId={campaign.id} step={step} onChange={onChange} />
          </li>
        ))}
      </ol>
    </Section>
  )
}

function StepReviewCard({
  campaignId,
  step,
  onChange,
}: {
  campaignId: number
  step: Step
  onChange: () => Promise<unknown>
}) {
  const [index, setIndex] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)
  const offset = Math.floor(index / STEP_PAGE) * STEP_PAGE
  const review = useQuery(stepReviewQuery(campaignId, step.id, offset))
  const onError = async (error: unknown) => {
    if (!isStale(error)) return
    setNotice(STALE_STEP)
    await onChange()
  }
  const approve = useMutation({
    mutationFn: (fingerprint: string) => approveStep(campaignId, step.id, fingerprint),
    onMutate: () => setNotice(null),
    onSuccess: onChange,
    onError,
  })
  const approveOne = useMutation({
    mutationFn: (message: MessagePreview) => approveMessages(campaignId, step.id, [message]),
    onMutate: () => setNotice(null),
    onSuccess: onChange,
    onError,
  })
  const title = `Step ${step.position}, ${step.channel}: ${step.template_name}`

  if (review.isPending) return <LoadingNote label={`Rendering step ${step.position}…`} />
  if (review.isError)
    return <ErrorNote label={`Step ${step.position} did not render.`} error={review.error} />
  const data = review.data
  const total = data.total
  const shown = total === 0 ? 0 : Math.min(index, total - 1)
  const message = data.messages[shown - data.offset] ?? null
  const go = (to: number) => setIndex(Math.max(0, Math.min(to, total - 1)))
  const refusal = [approve, approveOne].find((m) => m.isError && !isStale(m.error))?.error

  return (
    <section
      aria-label={`Step ${step.position}`}
      className="flex flex-col gap-2 rounded-lg border p-3 text-sm"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-medium">{title}</h4>
        <StepState
          approved={data.approved}
          perMessage={data.per_message}
          unapproved={data.unapproved}
        />
      </div>
      {data.per_message && (
        <Callout tone="warning">
          <p>
            This step&apos;s template uses {'{{ personal_line }}'}, so every message is different.
            Approve each message on its own.
          </p>
        </Callout>
      )}
      {total === 0 ? (
        <p className="text-muted-foreground">No message of this step can be sent.</p>
      ) : (
        <div
          role="group"
          aria-label={`Messages of step ${step.position}`}
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === 'ArrowLeft') {
              event.preventDefault()
              go(shown - 1)
            } else if (event.key === 'ArrowRight') {
              event.preventDefault()
              go(shown + 1)
            }
          }}
          className="flex flex-col gap-2 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <div className="flex items-center gap-2">
            <Button
              size="xs"
              variant="outline"
              aria-label="Previous message"
              disabled={shown === 0}
              onClick={() => go(shown - 1)}
            >
              <ChevronLeft aria-hidden="true" />
            </Button>
            <span aria-live="polite" className="text-muted-foreground">
              {shown + 1} of {total}
            </span>
            <Button
              size="xs"
              variant="outline"
              aria-label="Next message"
              disabled={shown >= total - 1}
              onClick={() => go(shown + 1)}
            >
              <ChevronRight aria-hidden="true" />
            </Button>
            <span className="text-xs text-muted-foreground">Use ← and → to page.</span>
          </div>
          {message === null ? (
            <LoadingNote label="Rendering…" />
          ) : (
            <MessageView
              message={message}
              perMessage={data.per_message}
              pending={approveOne.isPending}
              onApprove={() => approveOne.mutate(message)}
            />
          )}
        </div>
      )}
      {!data.per_message &&
        (data.approved ? (
          <p className="text-muted-foreground">
            Approved for these {total} {total === 1 ? 'message' : 'messages'} and any rendered
            later, until the step or its template changes.
          </p>
        ) : (
          <Button
            className="w-fit"
            size="sm"
            disabled={approve.isPending}
            onClick={() => approve.mutate(data.fingerprint)}
          >
            Approve step {step.position}
          </Button>
        ))}
      <StaleNotice notice={notice} />
      {refusal !== undefined && <ErrorNote label="Not approved." error={refusal} />}
      {data.blocked.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="font-medium">
            {data.blocked.length} {data.blocked.length === 1 ? 'message' : 'messages'} can&apos;t be
            sent and {data.blocked.length === 1 ? 'stays' : 'stay'} blocked
          </p>
          <ul aria-label={`Blocked messages of step ${step.position}`} className="text-xs">
            {data.blocked.map((m) => (
              <li key={m.enrollment_id}>
                {m.contact_name || 'Unnamed contact'}: {m.blocked}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function StepState({
  approved,
  perMessage,
  unapproved,
}: {
  approved: boolean
  perMessage: boolean
  unapproved: number
}) {
  if (perMessage) {
    return (
      <span className="text-xs text-muted-foreground">
        {unapproved === 0
          ? 'Every message approved'
          : `${unapproved} ${unapproved === 1 ? 'message' : 'messages'} to approve`}
      </span>
    )
  }
  return approved ? (
    <span className="text-xs font-medium text-emerald-700 dark:text-emerald-300">Approved</span>
  ) : (
    <span className="text-xs text-muted-foreground">Not approved</span>
  )
}

function MessageView({
  message,
  perMessage,
  pending,
  onApprove,
}: {
  message: MessagePreview
  perMessage: boolean
  pending: boolean
  onApprove: () => void
}) {
  return (
    <article
      aria-label={`Message to ${message.contact_name || 'unnamed contact'}`}
      className="rounded-md bg-muted/40 p-2"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {message.contact_name || 'Unnamed contact'}
          {message.to_address !== null && ` <${message.to_address}>`}
        </p>
        {perMessage &&
          message.blocked === null &&
          (message.approved ? (
            <span className="text-xs font-medium text-emerald-700 dark:text-emerald-300">
              Approved
            </span>
          ) : (
            <Button size="xs" disabled={pending} onClick={onApprove}>
              Approve this message
            </Button>
          ))}
      </div>
      {message.blocked !== null && <p className="text-destructive">Blocked: {message.blocked}</p>}
      {message.subject !== null && <p className="font-medium">{message.subject}</p>}
      {message.body !== null && <pre className="font-sans whitespace-pre-wrap">{message.body}</pre>}
      {message.issues.length > 0 && (
        <ul className="mt-1 text-xs text-amber-700 dark:text-amber-300">
          {message.issues.map((issue, i) => (
            <li key={i}>
              {issue.severity}: {issue.message}
            </li>
          ))}
        </ul>
      )}
    </article>
  )
}

function LintSection({
  campaignId,
  missing,
  onChange,
}: {
  campaignId: number
  missing: Missing[]
  onChange: () => Promise<unknown>
}) {
  const [result, setResult] = useState<LintResult | null>(null)
  const lint = useMutation({
    mutationFn: () => lintCampaign(campaignId),
    onSuccess: async (answer) => {
      setResult(answer)
      await onChange()
    },
  })
  return (
    <Section title="Lint">
      <p className="text-sm text-muted-foreground">
        Every step&apos;s template, checked again. Only a clean result is recorded.
        {!isMissing(missing, 'lint') && ' Clean.'}
      </p>
      <Button
        variant="outline"
        className="w-fit"
        onClick={() => lint.mutate()}
        disabled={lint.isPending}
      >
        Run lint
      </Button>
      {lint.isError && <ErrorNote label="Lint did not run." error={lint.error} />}
      {result !== null &&
        (result.clean ? (
          <p role="status" className="text-sm">
            Lint is clean.
          </p>
        ) : (
          <ul role="alert" className="text-sm text-destructive">
            {result.steps.flatMap((step) =>
              step.errors.map((issue, index) => (
                <li key={`${step.position}-${index}`}>
                  Step {step.position}, {issue.part}: {issue.message}
                </li>
              )),
            )}
          </ul>
        ))}
    </Section>
  )
}

function TestSendSection({
  campaign,
  missing,
  onChange,
}: {
  campaign: Campaign
  missing: Missing[]
  onChange: () => Promise<unknown>
}) {
  const [sent, setSent] = useState<Record<number, TestSend>>({})
  const [refused, setRefused] = useState<Record<number, string>>({})
  const untested = new Set(
    missing.filter((m) => m.requirement === 'test_sends').flatMap((m) => m.step_positions ?? []),
  )
  const send = useMutation({
    mutationFn: (stepId: number) => testSend(campaign.id, stepId),
    onMutate: (stepId) =>
      setRefused((current) => {
        const next = { ...current }
        delete next[stepId]
        return next
      }),
    onSuccess: async (answer, stepId) => {
      setSent((current) => ({ ...current, [stepId]: answer }))
      await onChange()
    },
    onError: (error, stepId) =>
      setRefused((current) => ({ ...current, [stepId]: errorText(error) })),
  })
  const emailSteps = campaign.steps.filter((s) => s.channel === 'email')
  const to = campaign.mailbox_email ?? 'the campaign mailbox'

  return (
    <Section title="Test sends">
      <p className="text-sm text-muted-foreground">
        Each email step goes once to your own address, {to}, with a [Test] subject, filled in with
        your own details from Settings, About you, never a contact&apos;s. It never goes to a
        contact and counts toward no cap. Arm Gmail in Settings first. Armed for drafts, the test is
        a draft in your Drafts, and netkeeper never sends it. Armed to send, it&apos;s sent to you.
        A reply you send from your own mailbox isn&apos;t counted; to test reply detection, reply
        from a different Gmail account.
      </p>
      {emailSteps.length === 0 ? (
        <p className="text-sm text-muted-foreground">No email steps, so no test send is needed.</p>
      ) : (
        <ul className="flex flex-col gap-2 text-sm">
          {emailSteps.map((step) => {
            const done = sent[step.id]
            const refusal = refused[step.id]
            return (
              <li key={step.id} className="flex flex-col gap-1">
                <div className="flex items-center gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={send.isPending}
                    onClick={() => send.mutate(step.id)}
                  >
                    Send a test of step {step.position}
                  </Button>
                  <span className="text-muted-foreground">
                    {step.template_name}
                    {untested.has(step.position) ? ', not tested yet' : ', tested'}
                  </span>
                </div>
                {done !== undefined && (
                  <p role="status">
                    {done.drafted
                      ? `Test draft to ${done.to_address} created in your Drafts at ${formatWhen(done.sent_at)}.`
                      : `Sent to ${done.to_address} at ${formatWhen(done.sent_at)}.`}
                  </p>
                )}
                {refusal !== undefined && (
                  <Callout tone="danger" title={`Step ${step.position}'s test was not sent.`}>
                    <p>{refusal}</p>
                  </Callout>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </Section>
  )
}

function GuardsSection({
  campaignId,
  summary,
  priorContact,
}: {
  campaignId: number
  summary: string
  priorContact: string | null
}) {
  const [open, setOpen] = useState(false)
  const details = useQuery({ ...guardDetailsQuery(campaignId), enabled: open })
  return (
    <Section title="Guard summary">
      <p className="text-sm">{summary}</p>
      {priorContact !== null && <p className="text-sm text-muted-foreground">{priorContact}.</p>}
      <p className="text-sm text-muted-foreground">
        For your information: activation doesn&apos;t wait on it. The guards check each contact
        again when each step fires, and skip anyone they exclude then.
      </p>
      <Button
        variant="outline"
        className="w-fit"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
      >
        {open ? 'Hide skipped contacts' : 'Show skipped contacts'}
      </Button>
      {open &&
        (details.isPending ? (
          <LoadingNote label="Loading the skipped contacts…" />
        ) : details.isError ? (
          <ErrorNote label="The skipped contacts are unavailable." error={details.error} />
        ) : details.data.skipped.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nobody is skipped.</p>
        ) : (
          <>
            <ul aria-label="Skipped contacts" className="flex flex-col gap-1 text-sm">
              {details.data.skipped.map((c) => (
                <li key={c.contact_id}>
                  <span className="font-medium">{c.name || `Contact ${c.contact_id}`}</span>
                  <span className="text-muted-foreground">: {c.reasons.join(', ')}</span>
                </li>
              ))}
            </ul>
            {details.data.skipped_total > details.data.skipped.length && (
              <p className="text-sm text-muted-foreground">
                Showing the first {details.data.skipped.length} of {details.data.skipped_total}{' '}
                skipped contacts.
              </p>
            )}
          </>
        ))}
    </Section>
  )
}

function ActivateSection({
  campaign,
  missing,
  onChange,
}: {
  campaign: Campaign
  missing: Missing[]
  onChange: () => Promise<unknown>
}) {
  const [open, setOpen] = useState(false)
  const [start, setStart] = useState<StartChoice>(UNCHOSEN)
  const activate = useMutation({
    mutationFn: () => {
      // Nothing chosen yet (the default still loading): the backend's own default.
      if (!start.now && start.value === '') return activateCampaign(campaign.id, null)
      const startsAt = startIso(start)
      if (startsAt === null) throw new Error('Choose a start date and time, or Now.')
      return activateCampaign(campaign.id, startsAt)
    },
    onSuccess: async () => {
      setOpen(false)
      await onChange()
    },
  })
  const refusedWith = activate.error instanceof CampaignApiError ? activate.error.missing : null
  const pending = campaign.enrollments.pending ?? 0

  return (
    <Section title="Activate">
      <p className="text-sm text-muted-foreground">
        {missing.length === 0
          ? 'Every requirement is met.'
          : `${missing.length} ${missing.length === 1 ? 'requirement is' : 'requirements are'} still missing. Activate is available once every one is met.`}
      </p>
      {missing.length > 0 && (
        <ul aria-label="Missing before activation" className="list-disc pl-5 text-sm">
          {missing.map((m, index) => (
            <li key={index}>
              {REQUIREMENT_LABELS[m.requirement] ?? m.requirement}: {missingText(m)}
            </li>
          ))}
        </ul>
      )}
      <Button
        className="w-fit"
        disabled={missing.length > 0}
        onClick={() => {
          activate.reset()
          setStart(UNCHOSEN)
          setOpen(true)
        }}
      >
        Activate
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Activate ${campaign.name}?`}
        confirmLabel="Activate campaign"
        confirmVariant="default"
        onConfirm={() => activate.mutateAsync()}
        pending={activate.isPending}
        error={activate.isError ? errorText(activate.error) : null}
      >
        <p>
          {pending} pending {pending === 1 ? 'enrollment becomes' : 'enrollments become'} active.
          Nothing is sent before the start. From then, step 1 fires for each of them, one at a time
          and under the daily caps; a batch the caps hold back goes on the next day at the same
          time. You can pause the campaign at any time.
        </p>
        <StartPicker campaignId={campaign.id} choice={start} onChange={setStart} />
        {refusedWith !== null && refusedWith.length > 0 && (
          <ul aria-label="Still missing" className="list-disc pl-5 text-foreground">
            {refusedWith.map((m, index) => (
              <li key={index}>
                {REQUIREMENT_LABELS[m.requirement] ?? m.requirement}: {missingText(m)}
              </li>
            ))}
          </ul>
        )}
      </ConfirmDialog>
    </Section>
  )
}
