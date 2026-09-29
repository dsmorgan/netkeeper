/**
 * The review gate on one screen (spec 11.8, P3-09's API).
 *
 * The checklist at the top is the API's own `missing` list, so it never claims
 * a requirement is met that the gate would refuse. Each section below records
 * one requirement: sampled previews approved for the fingerprint they were shown
 * with, any enrollment you searched for, lint, a test send per email step to your
 * own mailbox, and the guard summary acknowledged as shown. Activation asks
 * first, and a `409` shows what is still missing.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, CircleDashed } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Input } from '@/components/ui/input'
import { Callout, ErrorNote, LoadingNote } from '@/features/crm/controls'

import {
  CampaignApiError,
  acknowledgeGuards,
  activateCampaign,
  approvePreviews,
  campaignKeys,
  enrollmentsQuery,
  errorText,
  lintCampaign,
  reviewQuery,
  samplePreviews,
  startReview,
  testSend,
  viewPreviews,
  type Campaign,
  type EnrollmentPreview,
  type LintResult,
  type Missing,
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
            <SampleSection campaignId={id} missing={missing} onChange={refresh} />
            <SearchSection campaignId={id} missing={missing} onChange={refresh} />
            <LintSection campaignId={id} missing={missing} onChange={refresh} />
            <TestSendSection campaign={campaign} missing={missing} onChange={refresh} />
            <GuardsSection
              summary={review.data.guard_summary}
              acknowledged={review.data.guards_acknowledged}
              missing={missing}
              onAcknowledge={() => acknowledgeGuards(id, review.data)}
              onChange={refresh}
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

/** Merge newly rendered previews into those already shown, by enrollment. */
function merge(shown: EnrollmentPreview[], incoming: EnrollmentPreview[]) {
  const byId = new Map(shown.map((p) => [p.enrollment_id, p]))
  for (const p of incoming) byId.set(p.enrollment_id, p)
  return [...byId.values()]
}

/** Whether a refusal says the thing shown is no longer current (a stale fingerprint). */
function isStale(error: unknown): boolean {
  return error instanceof CampaignApiError && error.status === 409
}

const STALE_PREVIEW =
  'Something changed since this preview was shown (the contact, a template or the audience), ' +
  'so it was refreshed. Read it again before approving.'

/**
 * Previews on show and their approval. A 409 on approve means a fingerprint went
 * stale: the review is refreshed and `reload` renders the refused previews again,
 * so the next approval carries the fingerprint of what is on screen now.
 */
function usePreviewApproval(
  campaignId: number,
  onChange: () => Promise<unknown>,
  reload: (stale: EnrollmentPreview[], shown: EnrollmentPreview[]) => Promise<EnrollmentPreview[]>,
) {
  const [shown, setShown] = useState<EnrollmentPreview[]>([])
  const [notice, setNotice] = useState<string | null>(null)
  const approve = useMutation({
    mutationFn: (previews: EnrollmentPreview[]) => approvePreviews(campaignId, previews),
    onMutate: () => setNotice(null),
    onSuccess: async (_review, previews) => {
      const ids = new Set(previews.map((p) => p.enrollment_id))
      setShown((current) =>
        current.map((p) => (ids.has(p.enrollment_id) ? { ...p, approved: true } : p)),
      )
      await onChange()
    },
    onError: async (error, previews) => {
      if (!isStale(error)) return
      const ids = new Set(previews.map((p) => p.enrollment_id))
      let refreshed: EnrollmentPreview[]
      try {
        refreshed = await reload(previews, shown)
      } catch {
        // Nothing current to show for them: drop the stale ones rather than keep them.
        refreshed = shown.filter((p) => !ids.has(p.enrollment_id))
      }
      setShown(refreshed)
      setNotice(STALE_PREVIEW)
      await onChange()
    },
  })
  const refusal = approve.isError && !isStale(approve.error) ? approve.error : null
  return { shown, setShown, approve, notice, refusal }
}

function StaleNotice({ notice }: { notice: string | null }) {
  if (notice === null) return null
  return (
    <p role="status" className="rounded-lg bg-amber-500/10 px-3 py-2 text-sm">
      {notice}
    </p>
  )
}

function PreviewList({
  previews,
  onApprove,
  pending,
}: {
  previews: EnrollmentPreview[]
  onApprove: (previews: EnrollmentPreview[]) => void
  pending: boolean
}) {
  const waiting = previews.filter((p) => !p.approved)
  return (
    <div className="flex flex-col gap-2">
      {waiting.length > 1 && (
        <Button
          variant="outline"
          className="w-fit"
          disabled={pending}
          onClick={() => onApprove(waiting)}
        >
          Approve all {waiting.length} shown
        </Button>
      )}
      <ul className="flex flex-col gap-2">
        {previews.map((preview) => (
          <li
            key={preview.enrollment_id}
            aria-label={`Preview for ${preview.contact_name}`}
            className="rounded-lg border p-3 text-sm"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{preview.contact_name || 'Unnamed contact'}</span>
              <span className="flex items-center gap-2 text-xs text-muted-foreground">
                <span title="The fingerprint this preview is approved for">
                  fingerprint {preview.fingerprint.slice(0, 12)}
                </span>
                {preview.approved ? (
                  <span className="font-medium text-emerald-700 dark:text-emerald-300">
                    Approved
                  </span>
                ) : (
                  <Button size="xs" disabled={pending} onClick={() => onApprove([preview])}>
                    Approve
                  </Button>
                )}
              </span>
            </div>
            <ol className="mt-2 flex flex-col gap-2">
              {preview.steps.map((step) => (
                <li key={step.position} className="rounded-md bg-muted/40 p-2">
                  <p className="text-xs text-muted-foreground">
                    Step {step.position}, {step.channel}
                    {step.to_address !== null && ` to ${step.to_address}`}
                  </p>
                  {step.error !== null ? (
                    <p className="text-destructive">Does not render: {step.error}</p>
                  ) : (
                    <>
                      {step.subject !== null && <p className="font-medium">{step.subject}</p>}
                      <pre className="font-sans whitespace-pre-wrap">{step.body}</pre>
                    </>
                  )}
                  {step.issues.length > 0 && (
                    <ul className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                      {step.issues.map((issue, index) => (
                        <li key={index}>
                          {issue.severity}: {issue.message}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ol>
          </li>
        ))}
      </ul>
    </div>
  )
}

function SampleSection({
  campaignId,
  missing,
  onChange,
}: {
  campaignId: number
  missing: Missing[]
  onChange: () => Promise<unknown>
}) {
  const { shown, setShown, approve, notice, refusal } = usePreviewApproval(
    campaignId,
    onChange,
    // The draw is kept while the audience is, so this re-renders it with current
    // fingerprints; a changed audience gives a new draw and the old one goes.
    async () => (await samplePreviews(campaignId)).enrollments,
  )
  const sample = useMutation({
    mutationFn: () => samplePreviews(campaignId),
    onSuccess: async (previews) => {
      setShown(previews.enrollments)
      await onChange()
    },
  })
  return (
    <Section title="Sampled previews">
      <p className="text-sm text-muted-foreground">
        Up to 10 enrollments drawn at random. Read each rendered message and approve it. The draw
        stays the same while the audience does.
        {!isMissing(missing, 'sample_previews') && ' Done.'}
      </p>
      <Button
        variant="outline"
        className="w-fit"
        onClick={() => sample.mutate()}
        disabled={sample.isPending}
      >
        {shown.length === 0 ? 'Show the sample' : 'Show the sample again'}
      </Button>
      {sample.isError && <ErrorNote label="The sample was not drawn." error={sample.error} />}
      <StaleNotice notice={notice} />
      {refusal !== null && <ErrorNote label="Not approved." error={refusal} />}
      <PreviewList
        previews={shown}
        onApprove={(previews) => approve.mutate(previews)}
        pending={approve.isPending}
      />
    </Section>
  )
}

function SearchSection({
  campaignId,
  missing,
  onChange,
}: {
  campaignId: number
  missing: Missing[]
  onChange: () => Promise<unknown>
}) {
  const [q, setQ] = useState('')
  const [submitted, setSubmitted] = useState<string | null>(null)
  const results = useQuery({
    ...enrollmentsQuery(campaignId, submitted ?? '', 'pending', 0, 8),
    enabled: submitted !== null,
  })
  const { shown, setShown, approve, notice, refusal } = usePreviewApproval(
    campaignId,
    onChange,
    async (stale, current) => {
      const ids = new Set(stale.map((p) => p.enrollment_id))
      const kept = current.filter((p) => !ids.has(p.enrollment_id))
      const again = await viewPreviews(
        campaignId,
        stale.map((p) => p.enrollment_id),
      )
      return merge(kept, again.enrollments)
    },
  )
  const view = useMutation({
    mutationFn: (ids: number[]) => viewPreviews(campaignId, ids),
    onSuccess: async (previews) => {
      setShown((current) => merge(current, previews.enrollments))
      await onChange()
    },
  })
  const awaiting = missing
    .filter((m) => m.requirement === 'searched_previews')
    .flatMap((m) => m.enrollment_ids ?? [])
    .filter((eid) => !shown.some((p) => p.enrollment_id === eid))

  return (
    <Section title="Search for anyone">
      <p className="text-sm text-muted-foreground">
        Preview any enrolled contact you want to check. Each one you view must be approved too.
      </p>
      {awaiting.length > 0 && (
        <Callout tone="warning">
          <p>
            {awaiting.length} viewed {awaiting.length === 1 ? 'preview is' : 'previews are'} not
            approved yet.{' '}
            <Button
              variant="link"
              className="h-auto p-0"
              onClick={() => view.mutate(awaiting.slice(0, 50))}
            >
              Show {awaiting.length === 1 ? 'it' : 'them'}
            </Button>
          </p>
        </Callout>
      )}
      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          setSubmitted(q.trim())
        }}
      >
        <Input
          aria-label="Search enrollments by name or address"
          placeholder="Name or address"
          value={q}
          onChange={(event) => setQ(event.target.value)}
          className="max-w-xs"
        />
        <Button type="submit" variant="outline">
          Search
        </Button>
      </form>
      {results.isError && <ErrorNote label="The search failed." error={results.error} />}
      {results.isSuccess &&
        (results.data.items.length === 0 ? (
          <p className="text-sm text-muted-foreground">No pending enrollment matches.</p>
        ) : (
          <ul aria-label="Search results" className="flex flex-col gap-1 text-sm">
            {results.data.items.map((row) => (
              <li key={row.id} className="flex items-center gap-2">
                <span>{row.contact_name || 'Unnamed contact'}</span>
                {row.email !== null && <span className="text-muted-foreground">{row.email}</span>}
                <Button
                  size="xs"
                  variant="outline"
                  disabled={view.isPending}
                  onClick={() => view.mutate([row.id])}
                >
                  Preview
                </Button>
              </li>
            ))}
          </ul>
        ))}
      {view.isError && <ErrorNote label="The preview did not render." error={view.error} />}
      <StaleNotice notice={notice} />
      {refusal !== null && <ErrorNote label="Not approved." error={refusal} />}
      <PreviewList
        previews={shown}
        onApprove={(previews) => approve.mutate(previews)}
        pending={approve.isPending}
      />
    </Section>
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
        Each email step goes once to your own address, {to}, with a [Test] subject. It never goes to
        a contact and counts toward no cap. Gmail must be armed for send in Settings first.
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
                    Sent to {done.to_address} at {formatWhen(done.sent_at)}.
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
  summary,
  acknowledged,
  missing,
  onAcknowledge,
  onChange,
}: {
  summary: string
  acknowledged: string | null
  missing: Missing[]
  onAcknowledge: () => Promise<unknown>
  onChange: () => Promise<unknown>
}) {
  const [notice, setNotice] = useState<string | null>(null)
  const ack = useMutation({
    mutationFn: onAcknowledge,
    onMutate: () => setNotice(null),
    onSuccess: onChange,
    onError: async (error) => {
      if (!isStale(error)) return
      // The audience or the guard results moved: fetch the summary as it is now.
      setNotice(
        'The guard results changed since the summary was shown, so it was refreshed. ' +
          'Read it again before acknowledging.',
      )
      await onChange()
    },
  })
  const done = !isMissing(missing, 'guards') && acknowledged !== null
  return (
    <Section title="Guard summary">
      <p className="text-sm">{summary}</p>
      {done ? (
        <p className="text-sm text-muted-foreground">Acknowledged.</p>
      ) : (
        <Button
          variant="outline"
          className="w-fit"
          onClick={() => ack.mutate()}
          disabled={ack.isPending}
        >
          Acknowledge this summary
        </Button>
      )}
      <StaleNotice notice={notice} />
      {ack.isError && !isStale(ack.error) && (
        <ErrorNote label="Not acknowledged." error={ack.error} />
      )}
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
  const activate = useMutation({
    mutationFn: () => activateCampaign(campaign.id),
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
        onConfirm={() => activate.mutate()}
        pending={activate.isPending}
        error={activate.isError ? errorText(activate.error) : null}
      >
        <p>
          {pending} pending {pending === 1 ? 'enrollment becomes' : 'enrollments become'} active.
          Step 1 fires after its delay, inside the send window and under the caps, for each of them.
          You can pause the campaign at any time.
        </p>
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
