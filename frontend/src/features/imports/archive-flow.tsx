import { useMutation } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { importArchive } from './api'
import type { ArchiveKind } from './archive-kind'
import { ErrorNote, Note } from './notes'
import { StepNav } from './step-nav'
import { ARCHIVE_STEPS } from './steps'
import type {
  ArchiveConnectionCounts,
  ArchiveImportResult,
  ArchiveInvitationCounts,
  ArchiveMessageCounts,
} from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const KIND_TITLE: Record<ArchiveKind, string> = {
  archive: 'Recognized: a LinkedIn data archive',
  messages: 'Recognized: your LinkedIn message history, on its own',
  invitations: 'Recognized: your LinkedIn invitation history, on its own',
}

const KIND_EXPLANATION: Record<ArchiveKind, string> = {
  archive:
    'This is the zip LinkedIn emails you. netkeeper reads it directly — connections, messages, ' +
    'and invitations — with no column mapping and no file to unzip by hand.',
  messages:
    'This is one file out of the full archive, not the whole zip, so only your message history ' +
    'is read from it. It adds messages to contacts you already have; it never creates a new one.',
  invitations:
    'This is one file out of the full archive, not the whole zip, so only your invitation ' +
    'history is read from it. It adds invitations to contacts you already have; it never creates ' +
    'a new one.',
}

/**
 * Guidance for one of #124's 422s, keyed by a substring of its own message.
 *
 * Every case still ends with the backend's exact text (`ArchiveErrorNote`
 * below), so nothing here has to be the whole story — only enough to say what
 * to do next without the person having to parse a guard's own error message.
 */
const ARCHIVE_ERROR_GUIDANCE: ReadonlyArray<{
  test: RegExp
  headline: string
  body: string
}> = [
  {
    test: /no Connections\.csv, messages\.csv, or Invitations\.csv|not a LinkedIn archive zip/i,
    headline: "This doesn't look like a LinkedIn export",
    body:
      'netkeeper looked for a Connections, messages, or Invitations table and found none. Make ' +
      'sure this is the zip LinkedIn emailed you, or one of Connections.csv, messages.csv, ' +
      'Invitations.csv extracted from it — and not some other zip or CSV.',
  },
  {
    test: /unsafe path/i,
    headline: 'This zip has a file netkeeper will not open',
    body:
      "One of the files inside has a path netkeeper refuses to trust. That isn't how a real " +
      'LinkedIn export is put together — request a fresh export from LinkedIn and try that ' +
      'download instead of this file.',
  },
  {
    test: /member limit|byte limit|uncompressed|compresses \d|compression ratio|upload limit/i,
    headline: 'This file is bigger or stranger than a real LinkedIn export',
    body:
      'netkeeper refused it before opening it, as a precaution — a real export is nowhere near ' +
      'this large or this densely compressed. If this genuinely is your export, request a fresh ' +
      'copy from LinkedIn and try that download.',
  },
]

const DEFAULT_ARCHIVE_ERROR = {
  headline: "netkeeper couldn't read this file",
  body:
    "This doesn't look like a zip or a CSV netkeeper recognizes. Make sure you're uploading the " +
    'zip LinkedIn emailed you, or one of Connections.csv, messages.csv, Invitations.csv extracted ' +
    'from it.',
}

function archiveGuidance(detail: string): { headline: string; body: string } {
  return ARCHIVE_ERROR_GUIDANCE.find(({ test }) => test.test(detail)) ?? DEFAULT_ARCHIVE_ERROR
}

function ArchiveErrorNote({ detail, filename }: { detail: string; filename: string }) {
  const { headline, body } = archiveGuidance(detail)
  return (
    <>
      <ErrorNote>{headline}</ErrorNote>
      <Note tone="warn">
        <p>{body}</p>
        <p className="text-xs">
          {filename}: {detail}
        </p>
      </Note>
    </>
  )
}

interface ArchiveImportFlowProps {
  file: File
  kind: ArchiveKind
  /** Choosing a different file: back to the upload step, nothing sent. */
  onBack: () => void
  /** After a successful import: back to the upload step, ready for another. */
  onRestart: () => void
}

/**
 * The archive shape: recognize, confirm, import in one step, then report.
 *
 * Nothing is sent until the person presses Import — the recognition line above
 * it is the one chance to back out before this file touches the database
 * (spec 10.5, P1-21 item 2).
 */
export function ArchiveImportFlow({ file, kind, onBack, onRestart }: ArchiveImportFlowProps) {
  const upload = useMutation({
    mutationFn: () => importArchive(file),
  })

  if (upload.isSuccess) {
    return <ArchiveResult result={upload.data} onRestart={onRestart} />
  }

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <StepNav current="review" steps={ARCHIVE_STEPS} />
      <Card>
        <CardHeader>
          <CardTitle>{KIND_TITLE[kind]}</CardTitle>
          <CardDescription>{file.name}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <p>{KIND_EXPLANATION[kind]}</p>
          <Note>
            <p>
              There is no column mapping or candidate review for this file, because that pipeline
              has none: it goes straight into your contacts in one step, and the next screen says
              exactly what it did.
            </p>
          </Note>
          {upload.isError && (
            <ArchiveErrorNote detail={message(upload.error)} filename={file.name} />
          )}
        </CardContent>
      </Card>
      <div className="flex items-center gap-2">
        <Button variant="outline" onClick={onBack} disabled={upload.isPending}>
          Choose a different file
        </Button>
        <Button onClick={() => upload.mutate()} disabled={upload.isPending}>
          {upload.isPending ? 'Importing…' : 'Import'}
        </Button>
      </div>
    </div>
  )
}

/** One line naming whatever this import actually did, skipping what it didn't. */
function summary(result: ArchiveImportResult): string {
  const parts: string[] = []
  const { connections, messages, invitations } = result
  if (connections.created > 0) {
    parts.push(`${connections.created} new ${connections.created === 1 ? 'contact' : 'contacts'}`)
  }
  if (connections.updated > 0) {
    parts.push(`${connections.updated} updated`)
  }
  if (messages.added > 0) {
    parts.push(`${messages.added} message ${messages.added === 1 ? 'interaction' : 'interactions'}`)
  }
  if (invitations.added > 0) {
    parts.push(`${invitations.added} ${invitations.added === 1 ? 'invitation' : 'invitations'}`)
  }
  return parts.length > 0
    ? `${parts.join(', ')}.`
    : 'Nothing new — everything here was already there.'
}

function CountsList({ items }: { items: ReadonlyArray<[string, number]> }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

/** `Connections.csv`'s counts, only when the file carried any (`rows > 0`). */
function ConnectionsCard({ counts }: { counts: ArchiveConnectionCounts }) {
  if (counts.rows === 0) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Connections.csv</CardTitle>
        <CardDescription>Your contact list</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <CountsList
          items={[
            ['Rows in the file', counts.rows],
            ['New contacts', counts.created],
            ['Contacts updated', counts.updated],
            ['Had an email address', counts.with_email],
            ['No connection date', counts.undated],
            ['Nothing to import', counts.skipped],
          ]}
        />
        {counts.needs_review > 0 && (
          <Note tone="warn">
            <p>
              {counts.needs_review} more looked like someone you might already have, but not closely
              enough to be sure automatically, so {counts.needs_review === 1 ? 'it' : 'they'}{' '}
              {counts.needs_review === 1 ? "wasn't" : "weren't"} created or merged.
            </p>
            <p>
              To decide {counts.needs_review === 1 ? 'it' : 'each one'}, import another file: choose
              Connections.csv on its own next time, not the zip — that goes through mapping and
              candidate review, which this screen does not have.
            </p>
          </Note>
        )}
      </CardContent>
    </Card>
  )
}

/** `messages.csv`'s counts, only when the file carried any (`rows > 0`). */
function MessagesCard({ counts }: { counts: ArchiveMessageCounts }) {
  if (counts.rows === 0) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>messages.csv</CardTitle>
        <CardDescription>Message history, added to contacts you already have</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <CountsList
          items={[
            ['Rows in the file', counts.rows],
            ['Conversations found', counts.conversations],
            ['Matched to a contact', counts.attributed],
            ['Interactions added', counts.added],
            ['Sent by you', counts.outbound],
            ['Received', counts.inbound],
            ['Already recorded', counts.already_present],
            ['Not with a contact you have', counts.unknown_contact],
            ['Group conversations, skipped', counts.group_threads],
            ['No other person found', counts.no_counterpart],
            ['No date', counts.undated],
          ]}
        />
        {counts.no_owner > 0 && (
          <Note tone="warn">
            <p>
              {counts.no_owner} {counts.no_owner === 1 ? 'conversation' : 'conversations'} could not
              be read because netkeeper could not tell which participant was you. The rest of the
              file was still read.
            </p>
          </Note>
        )}
      </CardContent>
    </Card>
  )
}

/** `Invitations.csv`'s counts, only when the file carried any (`rows > 0`). */
function InvitationsCard({ counts }: { counts: ArchiveInvitationCounts }) {
  if (counts.rows === 0) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Invitations.csv</CardTitle>
        <CardDescription>Invitation history, added to contacts you already have</CardDescription>
      </CardHeader>
      <CardContent>
        <CountsList
          items={[
            ['Rows in the file', counts.rows],
            ['Invitations added', counts.added],
            ['Already recorded', counts.already_present],
            ['Not with a contact you have', counts.unknown_contact],
            ['No other person found', counts.no_counterpart],
            ['No date', counts.undated],
            ['No direction given', counts.undirected],
          ]}
        />
      </CardContent>
    </Card>
  )
}

/**
 * What the import did, file by file — so a headline number like "616 contacts"
 * traces back to Connections.csv rather than reading as one opaque total
 * (spec 10.5, P1-21 item 4).
 *
 * There is no run behind this the way a CSV import has one: the archive
 * endpoint writes straight into the database in its own transaction and never
 * creates an `import_run` row, so there is nothing here to open from history
 * or roll back later — this screen, right now, is the only record of it.
 */
function ArchiveResult({
  result,
  onRestart,
}: {
  result: ArchiveImportResult
  onRestart: () => void
}) {
  const ignored = result.ignored_files
  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <StepNav current="result" steps={ARCHIVE_STEPS} />
      <Card>
        <CardHeader>
          <CardTitle>Imported {result.filename}</CardTitle>
          <CardDescription>{summary(result)}</CardDescription>
        </CardHeader>
      </Card>

      <ConnectionsCard counts={result.connections} />
      <MessagesCard counts={result.messages} />
      <InvitationsCard counts={result.invitations} />

      {ignored.length > 0 && (
        <Note>
          <p className="font-medium">
            {ignored.length} other {ignored.length === 1 ? 'file' : 'files'} in the export{' '}
            {ignored.length === 1 ? "wasn't" : "weren't"} read.
          </p>
          <p>
            netkeeper reads Connections.csv, messages.csv, and Invitations.csv today. The rest of
            the export was left exactly as it was: {ignored.join(', ')}.
          </p>
        </Note>
      )}

      <div className="flex flex-wrap gap-2">
        <Button render={<Link to="/contacts" />}>See your contacts</Button>
        <Button variant="outline" onClick={onRestart}>
          Import another file
        </Button>
      </div>
    </div>
  )
}
