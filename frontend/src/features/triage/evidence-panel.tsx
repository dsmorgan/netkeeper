/**
 * The evidence beside the contact: what the answer to "have we met?" is made of.
 *
 * Everything shown here arrives in the same response as the contact (P1-09), so
 * the panel never fetches and never flickers between cards.
 *
 * Three fields come back as whatever was stored, HTML and all:
 * `evidence.messages.recent[].summary`, `evidence.timeline[].interaction.summary`,
 * and `contact.notes`. A LinkedIn InMail body is HTML and the archive can cut it
 * mid-tag (issue #75). All three now go through `toPlainText` — the notes with
 * their line breaks kept, since a person typed them and the paragraphs are
 * theirs. Notes used to be rendered as a raw string on the grounds that they are
 * user-authored rather than archive HTML, which is true but left markup pasted
 * into a note showing as visible text (issue #92). Nothing here is ever parsed
 * as Markdown, and `dangerouslySetInnerHTML` appears nowhere on this screen and
 * must not be added to it.
 */

import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'

import { formatDay, formatMinute, plural } from './format'
import { toPlainText, truncate } from './plain-text'
import type { Interaction, TimelineEntry, TriageCard } from './api'

const INBOUND_KINDS = new Set(['li_in', 'email_in'])

const KIND_LABELS: Record<string, string> = {
  note: 'Note',
  call: 'Call',
  meeting: 'Meeting',
  email_out: 'Email sent',
  email_in: 'Email received',
  li_out: 'LinkedIn sent',
  li_in: 'LinkedIn received',
  li_view: 'Profile view',
}

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind
}

export function EvidencePanel({ card }: { card: TriageCard }) {
  const { contact, evidence } = card
  const { messages } = evidence
  const notes = toPlainText(contact.notes, { keepLineBreaks: true })
  // A company the contact worked at with nobody else from the address book in
  // it is emitted with `contact_count: 0`. Ten past positions would then draw
  // ten rows that say nothing; only an overlap is evidence.
  const overlaps = evidence.shared_companies.filter((shared) => shared.contact_count > 0)

  return (
    <section aria-label="Evidence" className="flex min-w-0 flex-col gap-4 text-sm">
      <div>
        <h3 className="font-medium">Messages</h3>
        {messages.total === 0 ? (
          <p className="text-muted-foreground">
            No message history. Nothing here says the two of you have written to each other.
            {messages.invitations > 0 &&
              ` An invitation is on file, under the timeline; clicking Connect is not a conversation, so it is not counted here.`}
          </p>
        ) : (
          <>
            <p className="text-muted-foreground">
              {plural(messages.total, 'message', 'messages')} · {messages.inbound} in,{' '}
              {messages.outbound} out · {formatDay(messages.first_at)} to{' '}
              {formatDay(messages.last_at)}
              {messages.invitations > 0 &&
                ` · ${plural(messages.invitations, 'invitation', 'invitations')} besides, not counted here`}
            </p>
            <ul className="mt-2 flex flex-col gap-2">
              {messages.recent.map((message) => (
                <MessageRow key={message.id} message={message} />
              ))}
            </ul>
          </>
        )}
      </div>

      <Separator />

      <div>
        <h3 className="font-medium">Companies you have other contacts at</h3>
        <p className="text-xs text-muted-foreground">
          Overlap with the rest of your address book, not with your own history.
        </p>
        {overlaps.length === 0 ? (
          <p className="mt-1 text-muted-foreground">
            None of this person&apos;s companies has anyone else from your network in it.
          </p>
        ) : (
          <ul className="mt-1 flex flex-col gap-1">
            {overlaps.map((shared) => (
              <li key={shared.company} className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium">{shared.company}</span>
                <span className="text-muted-foreground">
                  {plural(shared.contact_count, 'other contact', 'other contacts')},{' '}
                  {shared.met_count} met
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <Separator />

      <div>
        <h3 className="font-medium">Timeline</h3>
        {evidence.timeline.length === 0 ? (
          <p className="text-muted-foreground">Nothing recorded yet.</p>
        ) : (
          <ul className="mt-1 flex flex-col gap-1">
            {evidence.timeline.map((entry) => (
              <TimelineRow key={timelineKey(entry)} entry={entry} />
            ))}
          </ul>
        )}
      </div>

      {notes !== null && (
        <>
          <Separator />
          <div>
            <h3 className="font-medium">Notes</h3>
            <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{notes}</p>
          </div>
        </>
      )}
    </section>
  )
}

function MessageRow({ message }: { message: Interaction }) {
  const body = toPlainText(message.summary)
  const inbound = INBOUND_KINDS.has(message.kind)
  // A body that is only an inline image reads as nothing here, which is not the
  // same as a message whose body was never stored. Saying which one it is costs
  // a line and stops "No body stored" from being a small lie (issue #92).
  const stored = (message.summary ?? '').trim() !== ''
  return (
    <li className="min-w-0 rounded-md bg-muted/40 px-2 py-1.5">
      <p className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
        <Badge variant="outline">{inbound ? 'From them' : 'From you'}</Badge>
        <span>{kindLabel(message.kind)}</span>
        <span>{formatMinute(message.at)}</span>
      </p>
      {body === null ? (
        <p className="text-muted-foreground italic">
          {stored ? 'No readable text in this message.' : 'No body stored.'}
        </p>
      ) : (
        <p className="mt-1 break-words">{truncate(body, 240)}</p>
      )}
    </li>
  )
}

function TimelineRow({ entry }: { entry: TimelineEntry }) {
  if (entry.kind === 'snapshot') {
    const { snapshot } = entry
    const job = [snapshot.current_title, snapshot.current_company].filter(Boolean).join(' at ')
    return (
      <li className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-muted-foreground">{formatDay(entry.at)}</span>
        <span>Seen as {job === '' ? (snapshot.headline ?? 'unchanged') : job}</span>
      </li>
    )
  }
  const summary = toPlainText(entry.interaction.summary)
  return (
    <li className="flex flex-wrap items-baseline gap-x-2">
      <span className="text-muted-foreground">{formatDay(entry.at)}</span>
      <span>{kindLabel(entry.interaction.kind)}</span>
      {summary !== null && (
        <span className="min-w-0 break-words text-muted-foreground">{truncate(summary, 120)}</span>
      )}
    </li>
  )
}

function timelineKey(entry: TimelineEntry): string {
  return entry.kind === 'snapshot'
    ? `snapshot-${entry.snapshot.id}`
    : `interaction-${entry.interaction.id}`
}
