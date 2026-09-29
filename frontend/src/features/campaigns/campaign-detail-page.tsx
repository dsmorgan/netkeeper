/**
 * One campaign (P3-11a): its steps and their progress, its audience, the review
 * gate while it is a draft or reviewing, its enrollments, and pause and resume.
 */
import { Link } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Facts } from '@/components/facts'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Callout, ErrorNote, LoadingNote } from '@/features/crm/controls'
import { listsQuery } from '@/features/crm/api'

import {
  CampaignApiError,
  ENROLLMENT_PAGE,
  campaignKeys,
  campaignQuery,
  enroll,
  enrollmentsQuery,
  pauseCampaign,
  resumeCampaign,
  type Campaign,
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
  formatWhen,
} from './format'
import { ReviewPanel } from './review-panel'

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

export function CampaignDetailPage({ campaignId }: { campaignId: number }) {
  const campaign = useQuery(campaignQuery(campaignId))

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
      <StepsCard campaign={data} />
      {reviewable && <AudienceCard campaign={data} />}
      {reviewable && <ReviewPanel campaign={data} />}
      <EnrollmentsCard campaignId={data.id} />
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
      </CardContent>
    </Card>
  )
}

function StepsCard({ campaign }: { campaign: Campaign }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Steps</CardTitle>
        <CardDescription>
          Fired counts every message a step made; sent, those that went out.
        </CardDescription>
      </CardHeader>
      <CardContent>
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
              <th scope="col" className="py-1 font-medium">
                Sent
              </th>
            </tr>
          </thead>
          <tbody>
            {campaign.steps.map((step) => (
              <tr key={step.id} className="border-t border-border/60">
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
                  {step.delay_days === 0 ? 'at once' : `after ${step.delay_days} days`},{' '}
                  {CONDITION_LABELS[step.condition].toLowerCase()}
                  {step.same_thread && ', same thread'}
                </td>
                <td className="py-2 pr-3 text-muted-foreground">{MODE_LABELS[step.mode]}</td>
                <td className="py-2 pr-3 tabular-nums">{step.fired}</td>
                <td className="py-2 tabular-nums">{step.sent}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  )
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
              {outcome.enrolled} enrolled, {outcome.already} already in, {outcome.excluded} excluded
              {outcome.removed > 0 && `, ${outcome.removed} removed by the new source`}.{' '}
              {outcome.pending} pending in all.
            </p>
            <p className="font-medium">{outcome.summary}</p>
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

function EnrollmentsCard({ campaignId }: { campaignId: number }) {
  const [q, setQ] = useState('')
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<EnrollmentStatus | ''>('')
  const [offset, setOffset] = useState(0)
  const rows = useQuery(enrollmentsQuery(campaignId, search, status, offset))

  return (
    <Card>
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
              setStatus(event.target.value as EnrollmentStatus | '')
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
        {rows.isPending ? (
          <LoadingNote label="Loading the enrollments…" />
        ) : rows.isError ? (
          <ErrorNote label="The enrollments are unavailable." error={rows.error} />
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
                    <td className="py-2 text-muted-foreground">{row.exit_reason ?? ''}</td>
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
