import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

import { gmailActivityQuery, type RecentMessage } from './api'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const STATUS_CLASSES: Record<string, string> = {
  sent: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  received: 'bg-sky-500/10 text-sky-700 dark:text-sky-300',
  drafted: 'bg-sky-500/10 text-sky-700 dark:text-sky-300',
  scheduled: 'bg-muted text-muted-foreground',
  discarded: 'bg-muted text-muted-foreground',
  bounced: 'bg-destructive/10 text-destructive',
  failed: 'bg-destructive/10 text-destructive',
}

function when(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime())
    ? iso
    : at.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function kind(row: RecentMessage): string {
  return row.direction === 'in' ? 'Reply' : 'Sent by you'
}

function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium',
        STATUS_CLASSES[status] ?? 'bg-muted text-muted-foreground',
      )}
    >
      {status}
    </span>
  )
}

function step(row: RecentMessage): string {
  const name = row.campaign_name ?? '—'
  return row.step_position === null ? name : `${name}, step ${row.step_position}`
}

/**
 * The newest email, sent or received, newest first. Gmail has no run history of its
 * own: a send is a message, and the reply poll keeps only its last time (shown by the
 * Reply poll card), so this is the record the app has. It never shows a subject or body.
 */
export function RecentCard() {
  const activity = useQuery(gmailActivityQuery)

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Recent email</CardTitle>
        <CardDescription>The latest sends, drafts and replies, newest first.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {activity.isPending && <p role="status">Loading…</p>}
        {activity.isError && <p role="alert">{message(activity.error)}</p>}
        {activity.isSuccess && activity.data.recent.length === 0 && (
          <p className="text-muted-foreground">No email yet.</p>
        )}
        {activity.isSuccess && activity.data.recent.length > 0 && (
          <>
            <ul className="space-y-2 sm:hidden" aria-label="Recent email">
              {activity.data.recent.map((row) => (
                <li key={row.id} className="rounded-lg border border-border/60 p-2">
                  <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
                    <span className="font-medium break-words">{row.contact_name}</span>
                    <StatusPill status={row.status} />
                  </div>
                  <p className="text-muted-foreground">
                    {kind(row)} · {when(row.at)}
                  </p>
                  <p className="text-muted-foreground break-words">{step(row)}</p>
                  {row.error !== null && (
                    <p className="text-xs break-words text-destructive">{row.error}</p>
                  )}
                </li>
              ))}
            </ul>
            <div className="hidden sm:block sm:overflow-x-auto">
              <table className="w-full min-w-max text-left">
                <thead className="text-muted-foreground">
                  <tr>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      When
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Kind
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Status
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Contact
                    </th>
                    <th scope="col" className="py-1 font-medium">
                      Campaign
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {activity.data.recent.map((row) => (
                    <tr key={row.id} className="border-t border-border/60 align-top">
                      <td className="py-2 pr-3 text-muted-foreground">{when(row.at)}</td>
                      <td className="py-2 pr-3">{kind(row)}</td>
                      <td className="py-2 pr-3">
                        <StatusPill status={row.status} />
                        {row.error !== null && (
                          <p className="mt-1 max-w-xs text-xs break-words text-destructive">
                            {row.error}
                          </p>
                        )}
                      </td>
                      <td className="py-2 pr-3">{row.contact_name}</td>
                      <td className="py-2 text-muted-foreground">{step(row)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
