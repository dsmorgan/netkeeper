/**
 * A campaign's results (#350): summary tiles for sent, reply rate, bounced and opted
 * out, and a bar chart of the sends per day (per week, or per few weeks, for a long
 * campaign). Each tile opens the enrollments table below, filtered by its status in
 * the URL, and moves focus to it. Plain CSS bars: the frontend has no chart library.
 */
import type { UseQueryResult } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ErrorNote, LoadingNote } from '@/features/crm/controls'

import type { CampaignResults, DaySends, EnrollmentStatus } from './api'
import { bucketSends, formatDay, formatRate } from './format'

/** The enrollments table's anchor, which the tiles link to. */
export const ENROLLMENTS_ANCHOR = 'enrollments'

export function ResultsCard({
  campaignId,
  results,
}: {
  campaignId: number
  results: UseQueryResult<CampaignResults>
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Results</CardTitle>
        <CardDescription>
          Sent counts every message that went out, bounced ones included. A reply or opt-out counts
          against the last step sent before it; a bounce, against the step that bounced.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-sm">
        {results.isPending ? (
          <LoadingNote label="Loading the results…" />
        ) : results.isError ? (
          <ErrorNote label="The results are unavailable." error={results.error} />
        ) : (
          <>
            <Tiles campaignId={campaignId} results={results.data} />
            <SendsChart days={results.data.sends_per_day} timezone={results.data.timezone} />
          </>
        )}
      </CardContent>
    </Card>
  )
}

function Tiles({ campaignId, results }: { campaignId: number; results: CampaignResults }) {
  const t = results.totals
  const tiles: ReadonlyArray<{
    label: string
    value: string
    detail: string
    status: EnrollmentStatus | ''
  }> = [
    {
      label: 'Sent',
      value: String(t.sent),
      detail: `to ${t.contacted} ${t.contacted === 1 ? 'contact' : 'contacts'}`,
      status: '',
    },
    {
      label: 'Reply rate',
      value: formatRate(t.reply_rate),
      detail: `${t.replied} replied`,
      status: 'replied',
    },
    { label: 'Bounced', value: String(t.bounced), detail: 'enrollments', status: 'bounced' },
    { label: 'Opted out', value: String(t.opted_out), detail: 'enrollments', status: 'opted_out' },
  ]
  return (
    <ul className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {tiles.map((tile) => (
        <li key={tile.label}>
          <Link
            to="/campaigns/$campaignId"
            params={{ campaignId: String(campaignId) }}
            search={tile.status === '' ? {} : { status: tile.status }}
            resetScroll={false}
            aria-label={`${tile.label}: ${tile.value}. Show ${
              tile.status === '' ? 'every enrollment' : 'these enrollments'
            }`}
            onClick={() => document.getElementById(ENROLLMENTS_ANCHOR)?.focus()}
            className="flex flex-col gap-0.5 rounded-lg border border-border p-3 hover:bg-muted/40"
          >
            <span className="text-muted-foreground">{tile.label}</span>
            <span className="text-2xl font-semibold tabular-nums">{tile.value}</span>
            <span className="text-muted-foreground">{tile.detail}</span>
          </Link>
        </li>
      ))}
    </ul>
  )
}

function unitOf(size: number): string {
  if (size === 1) return 'day'
  if (size === 7) return 'week'
  return `${size / 7} weeks`
}

export function SendsChart({ days, timezone }: { days: DaySends[]; timezone: string }) {
  if (days.length === 0) {
    return <p className="text-muted-foreground">Nothing sent yet, so no sends per day.</p>
  }
  const { days: size, buckets } = bucketSends(days)
  const unit = unitOf(size)
  const label = (start: string) => {
    if (size === 1) return formatDay(start)
    if (size === 7) return `Week of ${formatDay(start)}`
    return `${unit} from ${formatDay(start)}`
  }
  const most = Math.max(...buckets.map((b) => b.sent), 1)
  const total = buckets.reduce((sum, b) => sum + b.sent, 0)
  const first = buckets[0]
  const last = buckets[buckets.length - 1]
  return (
    <figure className="flex min-w-0 flex-col gap-1 overflow-x-auto">
      <figcaption className="text-muted-foreground">
        Sends per {unit} ({timezone}), most {most} in a {unit}
      </figcaption>
      <div
        role="img"
        aria-label={`Sends per ${unit}: ${total} over ${days.length} ${
          days.length === 1 ? 'day' : 'days'
        }, at most ${most} in a ${unit}`}
        className="flex h-24 items-end gap-px border-b border-border"
      >
        {buckets.map((b) => (
          <div
            key={b.start}
            data-testid="sends-bar"
            title={`${label(b.start)}: ${b.sent} sent`}
            className="min-w-[2px] flex-1 rounded-t-sm bg-chart-3"
            style={{ height: `${(b.sent / most) * 100}%` }}
          />
        ))}
      </div>
      {first !== undefined && last !== undefined && (
        <div
          data-testid="sends-axis"
          className="flex justify-between text-xs text-muted-foreground"
        >
          <span>{label(first.start)}</span>
          {buckets.length > 1 && <span>{label(last.start)}</span>}
        </div>
      )}
      <table className="sr-only">
        <caption>Sends per {unit}</caption>
        <thead>
          <tr>
            <th scope="col">{size === 1 ? 'Day' : 'From'}</th>
            <th scope="col">Sent</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((b) => (
            <tr key={b.start}>
              <th scope="row">{b.start}</th>
              <td>{b.sent}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
