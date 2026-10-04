/**
 * A campaign's results (#350): summary tiles for sent, reply rate, bounced and opted
 * out, and a bar chart of the sends per day. Each tile opens the enrollments table
 * below, filtered by its status. Plain CSS bars: the frontend has no chart library.
 */
import type { UseQueryResult } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ErrorNote, LoadingNote } from '@/features/crm/controls'

import type { CampaignResults, DaySends, EnrollmentStatus } from './api'
import { formatDay, formatRate } from './format'

/** The enrollments table's anchor, which the tiles link to. */
export const ENROLLMENTS_ANCHOR = 'enrollments'

export function ResultsCard({
  results,
  onShowEnrollments,
}: {
  results: UseQueryResult<CampaignResults>
  /** Filter the enrollments table: a status, or '' for every status. */
  onShowEnrollments: (status: EnrollmentStatus | '') => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Results</CardTitle>
        <CardDescription>
          Sent counts every message that went out, bounced ones included. A reply, bounce or opt-out
          counts against the last step sent before it.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-sm">
        {results.isPending ? (
          <LoadingNote label="Loading the results…" />
        ) : results.isError ? (
          <ErrorNote label="The results are unavailable." error={results.error} />
        ) : (
          <>
            <Tiles results={results.data} onShowEnrollments={onShowEnrollments} />
            <SendsChart days={results.data.sends_per_day} timezone={results.data.timezone} />
          </>
        )}
      </CardContent>
    </Card>
  )
}

function Tiles({
  results,
  onShowEnrollments,
}: {
  results: CampaignResults
  onShowEnrollments: (status: EnrollmentStatus | '') => void
}) {
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
          <a
            href={`#${ENROLLMENTS_ANCHOR}`}
            aria-label={`${tile.label}: ${tile.value}. Show ${
              tile.status === '' ? 'every enrollment' : 'these enrollments'
            }`}
            onClick={() => onShowEnrollments(tile.status)}
            className="flex flex-col gap-0.5 rounded-lg border border-border p-3 hover:bg-muted/40"
          >
            <span className="text-muted-foreground">{tile.label}</span>
            <span className="text-2xl font-semibold tabular-nums">{tile.value}</span>
            <span className="text-muted-foreground">{tile.detail}</span>
          </a>
        </li>
      ))}
    </ul>
  )
}

export function SendsChart({ days, timezone }: { days: DaySends[]; timezone: string }) {
  if (days.length === 0) {
    return <p className="text-muted-foreground">Nothing sent yet, so no sends per day.</p>
  }
  const most = Math.max(...days.map((d) => d.sent), 1)
  const total = days.reduce((sum, d) => sum + d.sent, 0)
  const first = days[0]
  const last = days[days.length - 1]
  return (
    <figure className="flex flex-col gap-1">
      <figcaption className="text-muted-foreground">
        Sends per day ({timezone}), most {most} in a day
      </figcaption>
      <div
        role="img"
        aria-label={`Sends per day: ${total} over ${days.length} ${
          days.length === 1 ? 'day' : 'days'
        }, at most ${most} in a day`}
        className="flex h-24 items-end gap-px border-b border-border"
      >
        {days.map((d) => (
          <div
            key={d.date}
            data-testid="sends-bar"
            title={`${formatDay(d.date)}: ${d.sent} sent`}
            className="min-w-[2px] flex-1 rounded-t-sm bg-chart-3"
            style={{ height: `${(d.sent / most) * 100}%` }}
          />
        ))}
      </div>
      {first !== undefined && last !== undefined && (
        <div className="flex justify-between text-xs text-muted-foreground">
          <span>{formatDay(first.date)}</span>
          {days.length > 1 && <span>{formatDay(last.date)}</span>}
        </div>
      )}
      <table className="sr-only">
        <caption>Sends per day</caption>
        <thead>
          <tr>
            <th scope="col">Day</th>
            <th scope="col">Sent</th>
          </tr>
        </thead>
        <tbody>
          {days.map((d) => (
            <tr key={d.date}>
              <th scope="row">{d.date}</th>
              <td>{d.sent}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
