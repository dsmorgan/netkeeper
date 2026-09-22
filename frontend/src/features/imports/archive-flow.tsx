import { useMutation } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useEffect, useRef } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { ApiError, importArchive } from './api'
import type { ArchiveKind } from './archive-kind'
import { ErrorNote, Note } from './notes'
import { StepNav } from './step-nav'
import { ARCHIVE_STEPS } from './steps'
import type {
  ArchiveConnectionCounts,
  ArchiveImportResult,
  ArchiveInvitationCounts,
  ArchiveMessageCounts,
  ArchiveRefusalCode,
} from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** `count` with `singular`/`plural` chosen to agree with it. */
function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return count === 1 ? singular : pluralForm
}

/** Moves focus to `ref`'s element once, when the screen it belongs to first appears. */
function useAnnounceOnMount(ref: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    ref.current?.focus()
    // Once, on mount: this is what tells a screen reader user the screen just
    // changed (spec 10.5, P1-21 review finding 9), not something to repeat on
    // every re-render (a pending state, a retry) while the person stays put.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
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

type Guidance = { headline: string; body: string }

/**
 * Guidance for a refused archive upload, keyed by the backend's own code.
 *
 * Every `422` from `POST /imports/archive` carries an `ArchiveRefusalCode`
 * beside its message (#124), so nothing here reads the message's words: a
 * reword on the backend used to send a person to the wrong next step, which
 * is what the substring matching this replaced did. `Record` over the
 * generated union, so a code the backend adds is a type error here rather
 * than a case that quietly falls through to the default.
 *
 * Several codes share one answer on purpose — a person who uploaded the wrong
 * file does not care whether it failed the member count or the size cap, only
 * what to do next — but the backend's own message is always shown underneath
 * (`ArchiveErrorNote`), so the specific reason is never lost.
 */
const NOT_A_LINKEDIN_EXPORT: Guidance = {
  headline: "This doesn't look like a LinkedIn export",
  body:
    'netkeeper looked for a Connections, messages, or Invitations table and found none. Make ' +
    'sure this is the zip LinkedIn emailed you, or one of Connections.csv, messages.csv, ' +
    'Invitations.csv extracted from it.',
}

const UNSAFE_MEMBER_PATH: Guidance = {
  headline: 'This zip has a file netkeeper will not open',
  body:
    "One of the files inside has a path netkeeper refuses to trust. That isn't how a real " +
    'LinkedIn export is put together — request a fresh export from LinkedIn and try that ' +
    'download instead of this file.',
}

const OVER_A_GUARD: Guidance = {
  headline: 'This file is bigger or stranger than a real LinkedIn export',
  body:
    'netkeeper refused it before opening it, as a precaution — a real export is nowhere near ' +
    'this large or this densely compressed. If this genuinely is your export, request a fresh ' +
    'copy from LinkedIn and try that download.',
}

const NESTED_ZIP: Guidance = {
  headline: 'This zip has another zip inside it',
  body: "That's one unzip too many for netkeeper to guess at safely — open this one and upload the file inside it instead of the outer zip.",
}

const DAMAGED: Guidance = {
  headline: "This file didn't come through in one piece",
  body: "It looks damaged or incomplete rather than the wrong file. Download the export again from LinkedIn's email and try that copy.",
}

const ENCRYPTED: Guidance = {
  headline: 'This zip is password-protected',
  body:
    'netkeeper cannot open it, and LinkedIn does not put a password on an export — so this is ' +
    'either a zip you made yourself or one from somewhere else. Upload the export as LinkedIn ' +
    'sent it.',
}

const MALFORMED_TABLE: Guidance = {
  headline: 'netkeeper found the table it wanted but could not read it',
  body:
    'One of the tables inside is missing a column netkeeper needs, or is not readable as text ' +
    'at all. If this file has been opened and re-saved by a spreadsheet, use the original ' +
    'download from LinkedIn instead.',
}

const KNOWN_CODES: Record<ArchiveRefusalCode, Guidance> = {
  not_a_zip: NOT_A_LINKEDIN_EXPORT,
  wrong_archive: NOT_A_LINKEDIN_EXPORT,
  nested_zip: NESTED_ZIP,
  encrypted: ENCRYPTED,
  damaged: DAMAGED,
  malformed_table: MALFORMED_TABLE,
  too_large: OVER_A_GUARD,
  too_many_members: OVER_A_GUARD,
  compression_ratio_too_high: OVER_A_GUARD,
  unsafe_member_path: UNSAFE_MEMBER_PATH,
}

/**
 * Never asserts what the file *is* — only that netkeeper couldn't use it — so
 * it stays honest for anything that reaches this screen without a code: a
 * failure from somewhere other than the archive endpoint's own refusals, or a
 * backend older than the code it sends. The backend's own text is always shown
 * right below it (`ArchiveErrorNote`), which is where the specific "what to
 * do" lives when nothing above knows better.
 */
const DEFAULT_ARCHIVE_ERROR: Guidance = {
  headline: "netkeeper couldn't import this file",
  body:
    "What it reported is below. If that doesn't say what to do, try downloading a fresh copy " +
    'from LinkedIn and use that, or double-check you picked the file this screen names.',
}

function isRefusalCode(code: string): code is ArchiveRefusalCode {
  return code in KNOWN_CODES
}

function archiveGuidance(error: unknown): Guidance {
  const code = error instanceof ApiError ? error.code : null
  if (code !== null && isRefusalCode(code)) return KNOWN_CODES[code]
  return DEFAULT_ARCHIVE_ERROR
}

function ArchiveErrorNote({ error, filename }: { error: unknown; filename: string }) {
  const { headline, body } = archiveGuidance(error)
  const detail = message(error)
  return (
    <>
      <ErrorNote>{headline}</ErrorNote>
      {/* `role="status"` so the one thing to do next is heard, not only the
          headline above it (review finding 9) — this is the whole point of
          `archiveGuidance`, and a silent `<div>` buried it before. */}
      <Note tone="warn" role="status">
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
  const headingRef = useRef<HTMLDivElement>(null)
  useAnnounceOnMount(headingRef)

  if (upload.isSuccess) {
    return <ArchiveResult result={upload.data} onRestart={onRestart} />
  }

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <StepNav current="review" steps={ARCHIVE_STEPS} />
      <Card>
        {/* `role="status"`: this line is the one chance to back out before
            anything is sent, and a screen reader user gets nothing today —
            the same silence review finding 9 measured on the result screen. */}
        <CardHeader role="status">
          <CardTitle
            ref={headingRef}
            tabIndex={-1}
            role="heading"
            aria-level={2}
            className="outline-none"
          >
            {KIND_TITLE[kind]}
          </CardTitle>
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
          {upload.isError && <ArchiveErrorNote error={upload.error} filename={file.name} />}
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

/**
 * One line naming whatever this import actually did.
 *
 * Never "nothing new" when the cards beneath it say otherwise (review finding
 * 1): a lone `messages.csv`/`Invitations.csv` can read hundreds of rows and
 * match none of them to a contact already here, and a `Connections.csv` can
 * resolve every row to a candidate it cannot write — both real, both distinct
 * from "everything here was already there," and both previously reported as
 * the same "nothing new" line as a genuine idempotent re-import.
 */
function summary(result: ArchiveImportResult): string {
  const { connections, messages, invitations } = result
  const parts: string[] = []
  if (connections.created > 0) {
    parts.push(`${connections.created} new ${plural(connections.created, 'contact')}`)
  }
  if (connections.updated > 0) {
    parts.push(`${connections.updated} ${plural(connections.updated, 'contact')} updated`)
  }
  if (connections.needs_review > 0) {
    parts.push(
      `${connections.needs_review} ${plural(connections.needs_review, 'needs', 'need')} a closer look`,
    )
  }
  if (messages.added > 0) {
    parts.push(`${messages.added} message ${plural(messages.added, 'interaction')}`)
  }
  if (invitations.added > 0) {
    parts.push(`${invitations.added} ${plural(invitations.added, 'invitation')}`)
  }
  if (parts.length > 0) return `${parts.join(', ')}.`

  if (messages.rows > 0 && messages.attributed === 0) {
    return (
      `Read ${messages.rows} message ${plural(messages.rows, 'row')}, but none of them matched ` +
      'a contact already here — import your connections first.'
    )
  }
  if (invitations.rows > 0 && invitations.added === 0 && invitations.already_present === 0) {
    return (
      `Read ${invitations.rows} invitation ${plural(invitations.rows, 'row')}, but none of them ` +
      'matched a contact already here — import your connections first.'
    )
  }
  if (connections.rows > 0 && connections.skipped === connections.rows) {
    return (
      `Read ${connections.rows} ${plural(connections.rows, 'row')} in Connections.csv, but none ` +
      'of them named anybody netkeeper could identify.'
    )
  }
  return 'Nothing new — everything here was already there.'
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
          // `role="status"`: the only route out of this state is the advice
          // in the second paragraph, which a screen reader user needs to
          // hear, not just find (review finding 9).
          <Note tone="warn" role="status">
            <p>
              {counts.needs_review} more looked like someone you might already have, but not closely
              enough to be sure automatically, so {plural(counts.needs_review, 'it', 'they')}{' '}
              {plural(counts.needs_review, "wasn't", "weren't")} created or merged.
            </p>
            <p>
              To decide {plural(counts.needs_review, 'it', 'each one')}: unzip the archive if you
              haven&rsquo;t already, then import the Connections.csv inside it on its own, not the
              zip — that goes through mapping and candidate review, which this screen does not have.
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
          <Note tone="warn" role="status">
            <p>
              {counts.no_owner} {plural(counts.no_owner, 'conversation')} could not be read because
              netkeeper could not tell which participant was you. The rest of the file was still
              read.
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
 * or roll back later — this screen, right now, is the only record of it. The
 * screen says so directly (review finding 6, tracked as #132 for the gap
 * itself): the Imports page puts a History tab right above this result, and
 * finding this import isn't in it is a worse way to learn that than being
 * told up front.
 */
function ArchiveResult({
  result,
  onRestart,
}: {
  result: ArchiveImportResult
  onRestart: () => void
}) {
  const ignored = result.ignored_files
  const headingRef = useRef<HTMLDivElement>(null)
  useAnnounceOnMount(headingRef)
  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <StepNav current="result" steps={ARCHIVE_STEPS} />
      <Card>
        <CardHeader role="status">
          <CardTitle
            ref={headingRef}
            tabIndex={-1}
            role="heading"
            aria-level={2}
            className="outline-none"
          >
            Imported {result.filename}
          </CardTitle>
          <CardDescription>{summary(result)}</CardDescription>
        </CardHeader>
        <CardContent>
          <Note>
            <p>
              Unlike a CSV import, this doesn&rsquo;t appear on the History tab and can&rsquo;t be
              rolled back from there — this screen is the only record of what it did.
            </p>
          </Note>
        </CardContent>
      </Card>

      <ConnectionsCard counts={result.connections} />
      <MessagesCard counts={result.messages} />
      <InvitationsCard counts={result.invitations} />

      {ignored.length > 0 && (
        <Note>
          <p className="font-medium">
            netkeeper also found {ignored.length} other {plural(ignored.length, 'table')} in the
            export and didn&rsquo;t read {plural(ignored.length, 'it', 'them')}.
          </p>
          <p>
            netkeeper reads Connections.csv, messages.csv, and Invitations.csv today; everything
            else it recognized as a table is listed here, untouched: {ignored.join(', ')}. (A real
            export usually has other files too — a profile file, assistant chat logs, anything that
            isn&rsquo;t a table — that don&rsquo;t show up in this list either way.)
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
