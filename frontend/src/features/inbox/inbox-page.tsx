import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'

import { FilterSummary, type FilterChip } from '@/components/filter-summary'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { EnrollmentStatusBadge } from '@/features/campaigns/badges'
import { formatWhen } from '@/features/campaigns/format'
import { contactsKeys } from '@/features/contacts/api'
import { EmptyState, ErrorNote, LoadingNote } from '@/features/crm/controls'
import { cn } from '@/lib/utils'

import {
  INBOX_PAGE,
  addNote,
  inboxKeys,
  inboxQuery,
  setHandled,
  type HandledFilter,
  type InboxItem,
  type InboxKind,
} from './api'

const KIND_LABELS: Record<InboxKind, string> = {
  reply: 'Reply',
  unsubscribe: 'Unsubscribe',
  bounce: 'Bounce',
}

const KIND_CLASSES: Record<InboxKind, string> = {
  reply: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  unsubscribe: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  bounce: 'bg-destructive/10 text-destructive',
}

function KindBadge({ kind }: { kind: InboxKind }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium whitespace-nowrap',
        KIND_CLASSES[kind],
      )}
    >
      {KIND_LABELS[kind]}
    </span>
  )
}

/**
 * What reply detection found (spec 11.7), newest first, to mark handled and
 * note on the contact (P3-11b). `enrollment` narrows it to one enrollment's,
 * which the campaign page links to.
 */
export function InboxPage({ enrollment }: { enrollment?: number }) {
  const [handled, setHandledFilter] = useState<HandledFilter>(
    enrollment === undefined ? 'unhandled' : 'all',
  )
  const [kind, setKind] = useState<InboxKind | ''>('')
  const [offset, setOffset] = useState(0)
  const [shownEnrollment, setShownEnrollment] = useState(enrollment)
  if (shownEnrollment !== enrollment) {
    // The enrollment chip went away, or a link named another: start from page one.
    setShownEnrollment(enrollment)
    setOffset(0)
  }
  const page = useQuery(inboxQuery({ handled, kind, enrollment, offset }))
  const navigate = useNavigate()
  const kindPicker = useRef<HTMLSelectElement>(null)
  // "Unhandled" or "All" is which inbox you are reading, not a filter on it, so
  // the count's whole is that inbox without a kind or an enrollment.
  const filtered = kind !== '' || enrollment !== undefined
  const whole = useQuery({
    ...inboxQuery({ handled, kind: '', offset: 0 }),
    enabled: filtered && page.isSuccess,
  })

  function clearKind() {
    setOffset(0)
    setKind('')
  }
  function clearEnrollment() {
    void navigate({ to: '/inbox' })
  }
  const chips: FilterChip[] = []
  if (kind !== '') {
    chips.push({
      key: 'kind',
      label: `kind: ${KIND_LABELS[kind].toLowerCase()}`,
      onRemove: clearKind,
    })
  }
  if (enrollment !== undefined) {
    const first = page.data?.items[0]
    chips.push({
      key: 'enrollment',
      label:
        first === undefined
          ? 'one enrollment'
          : `enrollment: ${first.contact_name || 'Unnamed contact'} in ${first.campaign_name}`,
      onRemove: clearEnrollment,
    })
  }
  function clearAll() {
    clearKind()
    if (enrollment !== undefined) clearEnrollment()
  }

  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        Replies, unsubscribes and bounces the campaigns detected, by email and on LinkedIn. Only the
        subject and a snippet are stored; open the thread in Gmail or the conversation on LinkedIn
        to read the rest.
      </p>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Select
          aria-label="Show"
          value={handled}
          onChange={(event) => {
            setOffset(0)
            setHandledFilter(event.target.value as HandledFilter)
          }}
        >
          <option value="unhandled">Unhandled</option>
          <option value="all">All</option>
        </Select>
        <Select
          ref={kindPicker}
          aria-label="Kind"
          value={kind}
          onChange={(event) => {
            setOffset(0)
            setKind(event.target.value as InboxKind | '')
          }}
        >
          <option value="">Every kind</option>
          {(Object.keys(KIND_LABELS) as InboxKind[]).map((k) => (
            <option key={k} value={k}>
              {KIND_LABELS[k]}
            </option>
          ))}
        </Select>
      </div>
      <FilterSummary
        shown={page.data?.total}
        total={whole.data?.total}
        chips={chips}
        onClear={clearAll}
        returnFocusTo={kindPicker}
      />
      {page.isPending ? (
        <LoadingNote label="Loading the inbox…" />
      ) : page.isError ? (
        <ErrorNote label="The inbox is unavailable." error={page.error} />
      ) : page.data.items.length === 0 && offset > 0 ? (
        // Handling the last unhandled item of a later page leaves that page empty.
        <p className="text-sm text-muted-foreground">
          Nothing left on this page.{' '}
          <Button variant="outline" size="sm" onClick={() => setOffset(0)}>
            Back to the first page
          </Button>
        </p>
      ) : page.data.total === 0 && filtered ? (
        <EmptyState title="Nothing matches these filters">
          <Button
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={() => {
              clearAll()
              kindPicker.current?.focus()
            }}
          >
            Clear filters
          </Button>
        </EmptyState>
      ) : page.data.total === 0 ? (
        <EmptyState title={handled === 'unhandled' ? 'Nothing to handle' : 'Nothing detected yet'}>
          {handled === 'unhandled'
            ? 'Every reply, unsubscribe and bounce has been handled.'
            : 'Replies and bounces to your campaigns appear here once the mailbox poll finds them.'}
        </EmptyState>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle level={2}>Inbox</CardTitle>
            <CardDescription>{page.data.unhandled} unhandled of every kind.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 text-sm">
            <ul className="flex flex-col">
              {page.data.items.map((item) => (
                <InboxRow key={item.id} item={item} />
              ))}
            </ul>
            {page.data.total > INBOX_PAGE && (
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => setOffset((current) => Math.max(current - INBOX_PAGE, 0))}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  disabled={offset + INBOX_PAGE >= page.data.total}
                  onClick={() => setOffset((current) => current + INBOX_PAGE)}
                >
                  Next
                </Button>
                <span className="text-muted-foreground">
                  {offset + 1}–{Math.min(offset + INBOX_PAGE, page.data.total)} of {page.data.total}
                </span>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function InboxRow({ item }: { item: InboxItem }) {
  const queryClient = useQueryClient()
  const [noting, setNoting] = useState(false)
  const [note, setNote] = useState('')
  const [noted, setNoted] = useState(false)

  const handle = useMutation({
    mutationFn: (handled: boolean) => setHandled(item.id, handled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: inboxKeys.all }),
  })
  const save = useMutation({
    mutationFn: () => addNote(item.contact_id, note.trim()),
    onSuccess: () => {
      setNoting(false)
      setNote('')
      setNoted(true)
      void queryClient.invalidateQueries({ queryKey: contactsKeys.timeline(item.contact_id) })
    },
  })
  const handledNow = item.handled_at !== null

  return (
    <li
      aria-label={`${KIND_LABELS[item.kind]} from ${item.contact_name || 'Unnamed contact'}`}
      className="flex flex-col gap-1.5 border-t border-border/60 py-3 first:border-t-0"
    >
      <div className="flex flex-wrap items-center gap-2">
        <KindBadge kind={item.kind} />
        <Link
          to="/contacts/$contactId"
          params={{ contactId: String(item.contact_id) }}
          className="font-medium underline underline-offset-4"
        >
          {item.contact_name || 'Unnamed contact'}
        </Link>
        <span className="text-muted-foreground">in</span>
        <Link
          to="/campaigns/$campaignId"
          params={{ campaignId: String(item.campaign_id) }}
          className="underline underline-offset-4"
        >
          {item.campaign_name}
        </Link>
        <EnrollmentStatusBadge status={item.enrollment_status} />
        <span className="ml-auto text-muted-foreground">{formatWhen(item.received_at)}</span>
      </div>
      <p className="font-medium">
        {item.kind === 'bounce' ? 'Bounced: ' : ''}
        {item.channel === 'linkedin' ? 'LinkedIn message' : (item.subject ?? '(no subject)')}
      </p>
      {/* Plain text: React escapes it, and nothing here renders HTML. */}
      {item.snippet !== null && item.snippet !== '' && (
        <p className="text-muted-foreground">{item.snippet}</p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant={handledNow ? 'outline' : 'default'}
          disabled={handle.isPending}
          onClick={() => handle.mutate(!handledNow)}
        >
          {handledNow ? 'Mark unhandled' : 'Mark handled'}
        </Button>
        {!noting && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setNoted(false)
              setNoting(true)
            }}
          >
            Add note
          </Button>
        )}
        {handledNow && (
          <span className="text-muted-foreground">Handled {formatWhen(item.handled_at)}</span>
        )}
        {noted && <span className="text-muted-foreground">Note added to the contact.</span>}
      </div>
      {handle.isError && <ErrorNote label="Could not update the item." error={handle.error} />}
      {noting && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (note.trim() !== '') save.mutate()
          }}
        >
          <Textarea
            aria-label={`Note on ${item.contact_name || 'the contact'}`}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            rows={3}
          />
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={note.trim() === '' || save.isPending}>
              Save note
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={() => setNoting(false)}>
              Cancel
            </Button>
          </div>
          {save.isError && <ErrorNote label="Could not add the note." error={save.error} />}
        </form>
      )}
    </li>
  )
}
