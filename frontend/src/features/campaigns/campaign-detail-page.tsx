/**
 * One campaign (P3-11a): its steps and their progress, its audience, the review
 * gate while it is a draft or reviewing, its enrollments, and pause and resume.
 * Its scheduled start, changeable until the first send, and each step's timing,
 * changeable until the campaign is over (#338). Once it is active, its results:
 * sends per day, and replies, bounces and opt-outs per step (#350). Its lifecycle
 * (#345): end a running campaign, archive and unarchive an ended one, delete one
 * that was never activated.
 */
import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'

import { Facts } from '@/components/facts'
import { FilterSummary, type FilterChip } from '@/components/filter-summary'
import { quoted } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Callout, EmptyState, ErrorNote, LoadingNote } from '@/features/crm/controls'
import { listsQuery } from '@/features/crm/api'
import { sendingHoursQuery } from '@/features/settings/api'
import { stepTimeWarning } from '@/features/settings/sending-hours'

import {
  CampaignApiError,
  ENROLLMENT_PAGE,
  archiveCampaign,
  campaignKeys,
  campaignQuery,
  campaignResultsQuery,
  deleteCampaign,
  deletePlanQuery,
  endCampaign,
  enroll,
  enrollmentsQuery,
  errorText,
  pauseCampaign,
  resumeCampaign,
  unarchiveCampaign,
  setCampaignStart,
  setStepSchedule,
  type Campaign,
  type CampaignResults,
  type Step,
  type StepResults,
  type EnrollOut,
  type EnrollmentStatus,
} from './api'
import { sourceBody, sourceProblem, type AudienceSource } from './audience'
import { AudiencePicker } from './audience-picker'
import { CampaignStatusBadge, EnrollmentStatusBadge } from './badges'
import {
  CONDITION_LABELS,
  ENROLLMENT_STATUSES,
  ENROLLMENT_STATUS_LABELS,
  MODE_LABELS,
  countsText,
  formatStart,
  formatWhen,
  timingText,
  toLocalInput,
} from './format'
import { ENROLLMENTS_ANCHOR, ResultsCard } from './results-card'
import { ReviewPanel } from './review-panel'
import { UNCHOSEN, startIso, type StartChoice } from './start'
import { StartPicker } from './start-picker'

export function NoSuchCampaign() {
  return (
    <Callout tone="warning" title="No such campaign">
      <p>
        <Link to="/campaigns" className="underline underline-offset-4">
          Back to the campaigns
        </Link>
      </p>
    </Callout>
  )
}

export function CampaignDetailPage({
  campaignId,
  status = '',
  onStatus = () => {},
}: {
  campaignId: number
  /** The enrollment table's status filter, '' for every status: the URL's `status`. */
  status?: EnrollmentStatus | ''
  onStatus?: (status: EnrollmentStatus | '') => void
}) {
  const campaign = useQuery(campaignQuery(campaignId))
  const results = useQuery(campaignResultsQuery(campaignId))

  if (campaign.isPending) return <LoadingNote label="Loading the campaign…" />
  if (campaign.isError) {
    if (campaign.error instanceof CampaignApiError && campaign.error.status === 404) {
      return <NoSuchCampaign />
    }
    return <ErrorNote label="The campaign is unavailable." error={campaign.error} />
  }
  const data = campaign.data
  const reviewable = data.status === 'draft' || data.status === 'reviewing'

  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <Overview campaign={data} />
      {!reviewable && (
        <ResultsCard campaignId={data.id} mailboxId={data.mailbox_id} results={results} />
      )}
      <StepsCard campaign={data} results={results.data} />
      {reviewable && <AudienceCard campaign={data} />}
      {reviewable && <ReviewPanel campaign={data} />}
      <EnrollmentsCard campaignId={data.id} status={status} onStatus={onStatus} />
    </div>
  )
}

function Overview({ campaign }: { campaign: Campaign }) {
  const queryClient = useQueryClient()
  const toggle = useMutation({
    mutationFn: () =>
      campaign.status === 'active' ? pauseCampaign(campaign.id) : resumeCampaign(campaign.id),
    onSuccess: (updated) => {
      queryClient.setQueryData(campaignKeys.one(campaign.id), updated)
      void queryClient.invalidateQueries({ queryKey: campaignKeys.list() })
    },
  })
  const canToggle = campaign.status === 'active' || campaign.status === 'paused'

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2} className="flex items-center gap-2">
          {campaign.name} <CampaignStatusBadge status={campaign.status} />
        </CardTitle>
        <CardDescription>{countsText(campaign.enrollments)}.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <Facts
          items={[
            ['Mailbox', campaign.mailbox_email],
            [
              'Starts',
              campaign.starts_at === null
                ? 'when you activate it'
                : formatStart(campaign.starts_at),
            ],
            ['Next send', formatWhen(campaign.next_action_at)],
            ['Daily cap', campaign.daily_cap ?? 'the mailbox’s'],
            ['Recent-contact guard', `${campaign.contacted_within_days_guard} days`],
            ['Approved', formatWhen(campaign.approved_at)],
          ]}
        />
        {canToggle && (
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              className="w-fit"
              onClick={() => toggle.mutate()}
              disabled={toggle.isPending}
            >
              {campaign.status === 'active' ? 'Pause' : 'Resume'}
            </Button>
            <span className="text-muted-foreground">
              {campaign.status === 'active'
                ? 'Nothing fires while paused; each enrollment keeps its place.'
                : 'A step that came due while paused fires at the next chance.'}
            </span>
          </div>
        )}
        {toggle.isError && <ErrorNote label="Not changed." error={toggle.error} />}
        {campaign.start_editable && <ChangeStart campaign={campaign} />}
        <Lifecycle campaign={campaign} />
      </CardContent>
    </Card>
  )
}

/**
 * End, archive, unarchive and delete (#345). End and delete ask first; archive is
 * undone by unarchive, so it does not.
 */
function Lifecycle({ campaign }: { campaign: Campaign }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [ending, setEnding] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const settle = (updated: Campaign) => {
    queryClient.setQueryData(campaignKeys.one(campaign.id), updated)
    void queryClient.invalidateQueries({ queryKey: campaignKeys.all })
  }
  const end = useMutation({
    mutationFn: () => endCampaign(campaign.id),
    onSuccess: (updated) => {
      settle(updated)
      setEnding(false)
    },
  })
  const archive = useMutation({
    mutationFn: () =>
      campaign.status === 'archived'
        ? unarchiveCampaign(campaign.id)
        : archiveCampaign(campaign.id),
    onSuccess: settle,
  })
  const remove = useMutation({
    mutationFn: () => deleteCampaign(campaign.id),
    onSuccess: async () => {
      setDeleting(false)
      queryClient.removeQueries({ queryKey: campaignKeys.one(campaign.id) })
      void queryClient.invalidateQueries({ queryKey: campaignKeys.list() })
      await navigate({ to: '/campaigns' })
    },
  })
  const running = campaign.status === 'active' || campaign.status === 'paused'
  // Archive takes only an ended campaign (#345): End first, then Archive appears.
  const archivable = campaign.status === 'completed' || campaign.status === 'archived'

  return (
    <>
      {campaign.status === 'archived' && (
        <Callout title="Archived">
          <p>
            Hidden from the campaign list and the dashboard. Its messages, results and contact
            timelines stay.
          </p>
        </Callout>
      )}
      {(running || archivable || campaign.deletable) && (
        <div className="flex flex-wrap items-center gap-2">
          {running && (
            <Button
              variant="outline"
              className="w-fit"
              onClick={() => {
                end.reset()
                setEnding(true)
              }}
            >
              End campaign
            </Button>
          )}
          {archivable && (
            <Button
              variant="outline"
              className="w-fit"
              disabled={archive.isPending}
              onClick={() => archive.mutate()}
            >
              {campaign.status === 'archived' ? 'Unarchive' : 'Archive'}
            </Button>
          )}
          {campaign.deletable && (
            <Button
              variant="destructive"
              className="w-fit"
              onClick={() => {
                remove.reset()
                setDeleting(true)
              }}
            >
              Delete campaign
            </Button>
          )}
          <span className="text-muted-foreground">
            {campaign.deletable
              ? 'It never sent anything, so you can delete it.'
              : campaign.status === 'archived'
                ? 'Unarchive brings it back to the list, as completed.'
                : archivable
                  ? 'Archive hides it from the list and the dashboard; you can unarchive it.'
                  : campaign.concluded
                    ? 'Every enrollment has finished. End it, then you can archive it.'
                    : 'End it to stop it for good; then you can archive it.'}
          </span>
        </div>
      )}
      {archive.isError && <ErrorNote label="Not changed." error={archive.error} />}
      <ConfirmDialog
        open={ending}
        onOpenChange={setEnding}
        title={`End ${campaign.name}?`}
        confirmLabel="End campaign"
        onConfirm={() => end.mutateAsync()}
        pending={end.isPending}
        error={end.isError ? errorText(end.error) : null}
      >
        <p>
          Nothing more is sent, and you cannot resume it. Each enrollment keeps its state, so a
          reply to a step already sent still counts in the results.
        </p>
        <p>
          A message already handed to Gmail is not recalled, and a Gmail draft stays yours to send
          or delete. Once it is ended, you can archive it.
        </p>
      </ConfirmDialog>
      {deleting && (
        <DeleteDialog
          campaign={campaign}
          onOpenChange={setDeleting}
          onConfirm={() => remove.mutateAsync()}
          pending={remove.isPending}
          error={remove.isError ? errorText(remove.error) : null}
        />
      )}
    </>
  )
}

function DeleteDialog({
  campaign,
  onOpenChange,
  onConfirm,
  pending,
  error,
}: {
  campaign: Campaign
  onOpenChange: (open: boolean) => void
  onConfirm: () => Promise<unknown>
  pending: boolean
  error: string | null
}) {
  const plan = useQuery(deletePlanQuery(campaign.id))
  const refusal = plan.data?.refusal ?? null
  return (
    <ConfirmDialog
      open
      onOpenChange={onOpenChange}
      title={`Delete ${campaign.name}?`}
      confirmLabel="Delete campaign"
      onConfirm={() => {
        if (plan.data === undefined || refusal !== null) return Promise.resolve()
        return onConfirm()
      }}
      pending={pending || plan.isPending}
      error={error ?? refusal ?? (plan.isError ? errorText(plan.error) : null)}
    >
      {plan.data === undefined ? (
        <p>Checking what the delete removes…</p>
      ) : (
        <>
          <p>
            Deletes the campaign, its {plan.data.steps} {plan.data.steps === 1 ? 'step' : 'steps'}{' '}
            and its {plan.data.enrollments}{' '}
            {plan.data.enrollments === 1 ? 'enrollment' : 'enrollments'}. Your templates stay. You
            cannot undo this.
          </p>
          {plan.data.leftover_drafts.length > 0 && (
            <>
              <p className="text-foreground">
                netkeeper never deletes a Gmail draft. These test drafts stay in Gmail; delete them
                by hand:
              </p>
              <ul aria-label="Gmail drafts left behind" className="list-disc pl-5">
                {plan.data.leftover_drafts.map((d) => (
                  <li key={d.gmail_draft_id}>
                    Step {d.step_position} test to {d.to_address}, drafted{' '}
                    {formatWhen(d.drafted_at)}
                  </li>
                ))}
              </ul>
            </>
          )}
          {plan.data.unverifies !== null && plan.data.unverifies !== undefined && (
            <p className="text-foreground">
              These are the only test drafts that could verify {plan.data.unverifies}. Once the
              campaign is deleted, the mailbox needs a new test draft from another campaign to be
              verified.
            </p>
          )}
        </>
      )}
    </ConfirmDialog>
  )
}

function ChangeStart({ campaign }: { campaign: Campaign }) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [start, setStart] = useState<StartChoice>(UNCHOSEN)
  const save = useMutation({
    mutationFn: () => {
      const startsAt = startIso(start)
      if (startsAt === null) throw new Error('Choose a start date and time, or Now.')
      return setCampaignStart(campaign.id, startsAt)
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(campaignKeys.one(campaign.id), updated)
      void queryClient.invalidateQueries({ queryKey: campaignKeys.all })
      setEditing(false)
    },
  })

  if (!editing) {
    return (
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          className="w-fit"
          onClick={() => {
            save.reset()
            setStart(
              campaign.starts_at === null
                ? UNCHOSEN
                : { now: false, value: toLocalValue(campaign.starts_at) },
            )
            setEditing(true)
          }}
        >
          Change start
        </Button>
        <span className="text-muted-foreground">You can change it until the first send.</span>
      </div>
    )
  }
  return (
    <section aria-label="Change start" className="flex flex-col gap-3">
      <StartPicker campaignId={campaign.id} choice={start} onChange={setStart} />
      <div className="flex gap-2">
        <Button className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
          Save start
        </Button>
        <Button variant="outline" onClick={() => setEditing(false)}>
          Cancel
        </Button>
      </div>
      {save.isError && <ErrorNote label="The start did not change." error={save.error} />}
    </section>
  )
}

const TIMING_EDITABLE = new Set<Campaign['status']>(['draft', 'reviewing', 'active', 'paused'])

function StepsCard({
  campaign,
  results,
}: {
  campaign: Campaign
  results: CampaignResults | undefined
}) {
  const [editing, setEditing] = useState<number | null>(null)
  const editable = TIMING_EDITABLE.has(campaign.status)
  const byStep = new Map(results?.steps.map((r) => [r.step_id, r]))
  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Steps</CardTitle>
        <CardDescription>
          Fired counts every message a step made; sent, those that went out. A reply or opt-out
          counts against the last step sent before it; a bounce, against the step that bounced. A
          step with no time of its own aims for the next suggested slot (Tue–Thu, 09:00 to 16:30)
          after its delay.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="relative overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-muted-foreground">
              <tr>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Step
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Template
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Timing
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Mode
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Fired
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Sent
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Replied
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Bounced
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Opted out
                </th>
                {editable && (
                  <th scope="col" className="py-1 font-medium">
                    <span className="sr-only">Edit</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {campaign.steps.map((step) => (
                <StepRow
                  key={step.id}
                  campaign={campaign}
                  step={step}
                  results={byStep.get(step.id)}
                  editable={editable}
                  editing={editing === step.id}
                  onEdit={(on) => setEditing(on ? step.id : null)}
                />
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}

function StepRow({
  campaign,
  step,
  results,
  editable,
  editing,
  onEdit,
}: {
  campaign: Campaign
  step: Step
  /** Undefined while the results load, or when they could not. */
  results: StepResults | undefined
  editable: boolean
  editing: boolean
  onEdit: (on: boolean) => void
}) {
  return (
    <>
      <tr className="border-t border-border/60">
        <th scope="row" className="py-2 pr-3 font-normal tabular-nums">
          {step.position}
        </th>
        <td className="py-2 pr-3">
          {step.template_name}{' '}
          <span className="text-muted-foreground">
            v{step.template_version}, {step.channel === 'email' ? 'email' : 'LinkedIn'}
          </span>
        </td>
        <td className="py-2 pr-3 text-muted-foreground">
          {timingText(step)}, {CONDITION_LABELS[step.condition].toLowerCase()}
          {step.same_thread && ', same thread'}
        </td>
        <td className="py-2 pr-3 text-muted-foreground">{MODE_LABELS[step.mode]}</td>
        <td className="py-2 pr-3 tabular-nums">{step.fired}</td>
        <td className="py-2 pr-3 tabular-nums">{step.sent}</td>
        <td className="py-2 pr-3 tabular-nums">{results?.replied ?? '—'}</td>
        <td className="py-2 pr-3 tabular-nums">{results?.bounced ?? '—'}</td>
        <td className="py-2 pr-3 tabular-nums">{results?.opted_out ?? '—'}</td>
        {editable && (
          <td className="py-2">
            <Button
              variant="outline"
              size="sm"
              aria-label={`Edit step ${step.position} timing`}
              onClick={() => onEdit(!editing)}
            >
              Timing
            </Button>
          </td>
        )}
      </tr>
      {editing && (
        <tr>
          <td colSpan={10} className="pb-3">
            <StepTimingForm campaign={campaign} step={step} onDone={() => onEdit(false)} />
          </td>
        </tr>
      )}
    </>
  )
}

function StepTimingForm({
  campaign,
  step,
  onDone,
}: {
  campaign: Campaign
  step: Step
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [days, setDays] = useState(String(step.delay_days))
  const [time, setTime] = useState(step.send_time ?? '')
  const save = useMutation({
    mutationFn: () =>
      setStepSchedule(campaign.id, step.id, Number(days), time === '' ? null : time),
    onSuccess: (updated) => {
      queryClient.setQueryData(campaignKeys.one(campaign.id), updated)
      void queryClient.invalidateQueries({ queryKey: campaignKeys.all })
      onDone()
    },
  })
  const valid = /^\d+$/.test(days) && Number(days) <= 365
  const hours = useQuery(sendingHoursQuery)
  const outside = stepTimeWarning(hours.data, time)
  const reviewing = campaign.status === 'draft' || campaign.status === 'reviewing'
  return (
    <form
      aria-label={`Step ${step.position} timing`}
      className="flex flex-col gap-2 rounded-lg bg-muted/40 p-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (valid) save.mutate()
      }}
    >
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span>{step.position === 1 ? 'Days after the start' : 'Days after the step before'}</span>
          <Input
            type="number"
            min={0}
            max={365}
            value={days}
            onChange={(event) => setDays(event.target.value)}
            className="w-24"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span>Time of day (leave empty for the next suggested slot)</span>
          <Input
            type="time"
            value={time}
            onChange={(event) => setTime(event.target.value)}
            className="w-fit"
          />
        </label>
        <Button type="submit" disabled={!valid || save.isPending}>
          Save timing
        </Button>
        <Button type="button" variant="outline" onClick={onDone}>
          Cancel
        </Button>
      </div>
      {outside !== null && (
        <Callout tone="warning" title="Outside the sending hours">
          <p>{outside}</p>
        </Callout>
      )}
      <p className="text-muted-foreground">
        {reviewing
          ? 'Changing the timing means the review records that name this step need doing again.'
          : 'Contacts already waiting for this step are due again by the new timing.'}
      </p>
      {save.isError && <ErrorNote label="The timing did not change." error={save.error} />}
    </form>
  )
}

function toLocalValue(iso: string): string {
  return toLocalInput(iso)
}

function currentSource(campaign: Campaign, listName: string | undefined): string {
  if (campaign.source_list_id !== null) {
    return `the list ${listName ?? `#${campaign.source_list_id}`}`
  }
  if (campaign.filter !== null) return 'a filter'
  return 'nothing yet'
}

function AudienceCard({ campaign }: { campaign: Campaign }) {
  const queryClient = useQueryClient()
  const lists = useQuery(listsQuery)
  const [outcome, setOutcome] = useState<EnrollOut | null>(null)
  const [changing, setChanging] = useState(false)
  const [source, setSource] = useState<AudienceSource>({ kind: 'list', listId: null })
  const hasSource = campaign.source_list_id !== null || campaign.filter !== null
  const draft = campaign.status === 'draft'

  const run = useMutation({
    mutationFn: (replace: boolean) => enroll(campaign.id, replace ? sourceBody(source) : {}),
    onSuccess: async (answer) => {
      setOutcome(answer)
      setChanging(false)
      await queryClient.invalidateQueries({ queryKey: campaignKeys.all })
    },
  })
  const listName = lists.data?.find((l) => l.id === campaign.source_list_id)?.name
  const problem = sourceProblem(source)

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Audience</CardTitle>
        <CardDescription>
          From {currentSource(campaign, listName)}. Enrolling runs every contact through the guards;
          only those they pass join, as pending.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {outcome !== null && (
          <div role="status" className="flex flex-col gap-1">
            <p>
              {outcome.enrolled} enrolled, {outcome.already} already in, {outcome.excluded} skipped
              {outcome.removed > 0 && `, ${outcome.removed} removed by the new source`}.{' '}
              {outcome.pending} pending in all.
            </p>
            <p className="font-medium">{outcome.summary}</p>
            {outcome.excluded > 0 && (
              <p className="text-muted-foreground">This time: {outcome.excluded_summary}</p>
            )}
          </div>
        )}
        {hasSource && !changing && (
          <Button className="w-fit" onClick={() => run.mutate(false)} disabled={run.isPending}>
            {outcome === null ? 'Enroll the audience' : 'Enroll again'}
          </Button>
        )}
        {draft ? (
          changing || !hasSource ? (
            <div className="flex flex-col gap-3">
              {hasSource && (
                <Callout tone="warning" title="Changing the source replaces the audience.">
                  <p>
                    Pending enrollments whose contacts the new source does not hold are removed, and
                    the new source&apos;s contacts are enrolled through the guards.
                  </p>
                </Callout>
              )}
              <AudiencePicker value={source} onChange={setSource} allowNone={false} />
              <div className="flex gap-2">
                <Button
                  className="w-fit"
                  disabled={problem !== null || run.isPending}
                  onClick={() => run.mutate(true)}
                >
                  {hasSource ? 'Replace the audience and enroll' : 'Enroll this audience'}
                </Button>
                {hasSource && (
                  <Button variant="outline" onClick={() => setChanging(false)}>
                    Cancel
                  </Button>
                )}
              </div>
            </div>
          ) : (
            <Button variant="outline" className="w-fit" onClick={() => setChanging(true)}>
              Change the source
            </Button>
          )
        ) : (
          <p className="text-muted-foreground">
            The source is fixed while the campaign is under review.
          </p>
        )}
        {run.isError && <ErrorNote label="Nobody was enrolled." error={run.error} />}
      </CardContent>
    </Card>
  )
}

function EnrollmentsCard({
  campaignId,
  status,
  onStatus,
}: {
  campaignId: number
  /** The status filter; the results tiles set it too. */
  status: EnrollmentStatus | ''
  onStatus: (status: EnrollmentStatus | '') => void
}) {
  const [q, setQ] = useState('')
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [shownStatus, setShownStatus] = useState(status)
  if (shownStatus !== status) {
    // A tile changed the filter: start from the first page of it.
    setShownStatus(status)
    setOffset(0)
  }
  const rows = useQuery(enrollmentsQuery(campaignId, search, status, offset))
  const filtered = search !== '' || status !== ''
  // The campaign's whole count, for "12 of 87": one row of the unfiltered list.
  const everyone = useQuery({
    ...enrollmentsQuery(campaignId, '', '', 0, 1),
    enabled: filtered && rows.isSuccess,
  })
  const searchBox = useRef<HTMLInputElement>(null)

  function clearSearch() {
    setQ('')
    setSearch('')
    setOffset(0)
  }
  function clearAll() {
    clearSearch()
    onStatus('')
  }
  const chips: FilterChip[] = []
  if (search !== '') chips.push({ key: 'q', label: quoted(search), onRemove: clearSearch })
  if (status !== '') {
    chips.push({
      key: 'status',
      label: `status: ${ENROLLMENT_STATUS_LABELS[status].toLowerCase()}`,
      onRemove: () => {
        setOffset(0)
        onStatus('')
      },
    })
  }

  return (
    <Card id={ENROLLMENTS_ANCHOR} tabIndex={-1} className="scroll-mt-4 outline-none">
      <CardHeader>
        <CardTitle level={2}>Enrollments</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            setOffset(0)
            setSearch(q.trim())
          }}
        >
          <Input
            ref={searchBox}
            aria-label="Find an enrollment"
            placeholder="Name or address"
            value={q}
            onChange={(event) => setQ(event.target.value)}
            className="max-w-xs"
          />
          <Select
            aria-label="Status"
            value={status}
            onChange={(event) => {
              setOffset(0)
              onStatus(event.target.value as EnrollmentStatus | '')
            }}
          >
            <option value="">Every status</option>
            {ENROLLMENT_STATUSES.map((s) => (
              <option key={s} value={s}>
                {ENROLLMENT_STATUS_LABELS[s]}
              </option>
            ))}
          </Select>
          <Button type="submit" variant="outline">
            Find
          </Button>
        </form>
        <FilterSummary
          shown={rows.data?.total}
          total={everyone.isError ? null : everyone.data?.total}
          chips={chips}
          onClear={clearAll}
          returnFocusTo={searchBox}
        />
        {rows.isPending ? (
          <LoadingNote label="Loading the enrollments…" />
        ) : rows.isError ? (
          <ErrorNote label="The enrollments are unavailable." error={rows.error} />
        ) : rows.data.total === 0 && filtered ? (
          <EmptyState title="No enrollments match these filters">
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={() => {
                clearAll()
                searchBox.current?.focus()
              }}
            >
              Clear filters
            </Button>
          </EmptyState>
        ) : rows.data.total === 0 ? (
          <p className="text-muted-foreground">No enrollments.</p>
        ) : (
          <>
            <table className="w-full text-left">
              <thead className="text-muted-foreground">
                <tr>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Contact
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Status
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Step
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Next action
                  </th>
                  <th scope="col" className="py-1 font-medium">
                    Note
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.data.items.map((row) => (
                  <tr key={row.id} className="border-t border-border/60">
                    <th scope="row" className="py-2 pr-3 font-normal">
                      <Link
                        to="/contacts/$contactId"
                        params={{ contactId: String(row.contact_id) }}
                        className="underline underline-offset-4"
                      >
                        {row.contact_name || 'Unnamed contact'}
                      </Link>
                      {row.email !== null && (
                        <span className="ml-2 text-muted-foreground">{row.email}</span>
                      )}
                    </th>
                    <td className="py-2 pr-3">
                      <EnrollmentStatusBadge status={row.status} />
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{row.current_step ?? '—'}</td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {formatWhen(row.next_action_at)}
                    </td>
                    <td className="py-2 text-muted-foreground">
                      {row.exit_reason ?? ''}
                      {row.not_sent_error != null && (
                        <span role="note" className="block text-destructive">
                          {row.not_sent_error}
                        </span>
                      )}
                      {(row.replied_at !== null || row.status === 'bounced') && (
                        <Link
                          to="/inbox"
                          search={{ enrollment: row.id }}
                          className="ml-2 underline underline-offset-4"
                        >
                          View in inbox
                        </Link>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.data.total > ENROLLMENT_PAGE && (
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => setOffset((current) => Math.max(current - ENROLLMENT_PAGE, 0))}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  disabled={offset + ENROLLMENT_PAGE >= rows.data.total}
                  onClick={() => setOffset((current) => current + ENROLLMENT_PAGE)}
                >
                  Next
                </Button>
                <span className="text-muted-foreground">
                  {offset + 1}–{Math.min(offset + ENROLLMENT_PAGE, rows.data.total)} of{' '}
                  {rows.data.total}
                </span>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
