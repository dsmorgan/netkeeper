import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ArrowLeft, ExternalLink, Mail } from 'lucide-react'
import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button, buttonVariants } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Facts } from '@/components/facts'
import { cn } from '@/lib/utils'

import { contactQuery, setArchived } from './api'
import { ContactChildren } from './contact-children'
import { ContactTags } from './contact-tags'
import { ContactTimeline } from './contact-timeline'
import {
  DoNotContactEditor,
  FieldEditor,
  MetEditor,
  NotesEditor,
  type EditableField,
} from './field-editor'
import { useContactWrite } from './use-contact-write'
import { displayName, formatDate, formatDateTime, gmailSearchUrl } from './format'
import { WriteError } from './merged-notice'
import { MET_LABELS } from './types'

/** The fields you can type into, in the order the screen shows them. */
const EDITABLE: ReadonlyArray<{ field: EditableField; label: string; kind?: 'date' }> = [
  { field: 'preferred_name', label: 'Preferred name' },
  { field: 'first_name', label: 'First name' },
  { field: 'last_name', label: 'Last name' },
  { field: 'headline', label: 'Headline' },
  { field: 'current_title', label: 'Title' },
  { field: 'current_company', label: 'Company' },
  { field: 'location', label: 'Location' },
  { field: 'connected_on', label: 'Connected on', kind: 'date' },
  { field: 'li_public_id', label: 'LinkedIn id' },
  { field: 'do_not_contact_reason', label: 'Do-not-contact reason' },
]

/**
 * A link out to another system, or a disabled button saying why there is none.
 * Never a live control that fails on click.
 */
function ExternalAction({
  href,
  icon,
  label,
  unavailable,
}: {
  href: string | null | undefined
  icon: ReactNode
  label: string
  unavailable: string
}) {
  if (!href) {
    return (
      <Button size="sm" variant="outline" disabled title={unavailable}>
        {icon}
        {label}
      </Button>
    )
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className={cn(buttonVariants({ variant: 'outline', size: 'sm' }))}
    >
      {icon}
      {label}
    </a>
  )
}

export function ContactDetailPage({ contactId }: { contactId: number }) {
  const detail = useQuery(contactQuery(contactId))
  const archive = useContactWrite(contactId)

  if (detail.isPending) {
    return <p className="text-muted-foreground">Loading contact…</p>
  }
  if (detail.isError) {
    return (
      <div role="alert" className="grid justify-items-start gap-3">
        <p className="text-destructive">This contact could not be loaded: {detail.error.message}</p>
        <Link to="/contacts" className={cn(buttonVariants({ variant: 'outline', size: 'sm' }))}>
          <ArrowLeft data-icon="inline-start" />
          Back to contacts
        </Link>
      </div>
    )
  }

  const contact = detail.data
  const name = displayName(contact)
  const archived = contact.archived_at !== null
  const email = contact.emails.find((row) => row.is_primary)?.email ?? contact.emails[0]?.email

  return (
    <div className="grid max-w-5xl gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Link to="/contacts" className={cn(buttonVariants({ variant: 'ghost', size: 'sm' }))}>
          <ArrowLeft data-icon="inline-start" />
          Contacts
        </Link>
        <h2 className="font-heading text-lg font-medium">{name}</h2>
        <Badge variant={contact.met === 'met' ? 'default' : 'outline'}>
          {MET_LABELS[contact.met]}
        </Badge>
        {contact.do_not_contact && <Badge variant="destructive">Do not contact</Badge>}
        {archived && <Badge variant="outline">Archived</Badge>}
        <div className="ml-auto flex items-center gap-2">
          <ExternalAction
            href={contact.li_url}
            icon={<ExternalLink data-icon="inline-start" />}
            label="Open on LinkedIn"
            unavailable="This contact has no LinkedIn URL"
          />
          <ExternalAction
            href={gmailSearchUrl(email)}
            icon={<Mail data-icon="inline-start" />}
            label="Open in Gmail"
            unavailable="This contact has no email address"
          />
          <Button
            size="sm"
            variant="outline"
            disabled={archive.isPending}
            onClick={() => archive.mutate(() => setArchived(contact.id, !archived))}
          >
            {archived ? 'Unarchive' : 'Archive'}
          </Button>
        </div>
      </div>

      {contact.resolved_from !== null && (
        <p role="status" className="rounded-lg bg-muted/60 px-3 py-2">
          Contact {contact.resolved_from} was merged into this one, so you were brought here
          instead.
        </p>
      )}
      {contact.merged_into_id !== null && (
        <p role="status" className="rounded-lg bg-muted/60 px-3 py-2">
          This contact was merged into{' '}
          <Link
            to="/contacts/$contactId"
            params={{ contactId: String(contact.merged_into_id) }}
            className="underline underline-offset-4"
          >
            contact {contact.merged_into_id}
          </Link>
          .
        </p>
      )}
      <WriteError error={archive.error} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card size="sm">
          <CardHeader>
            <CardTitle>Fields</CardTitle>
            <CardDescription>
              A LinkedIn field you edit here is a manual override: no later sync or import
              overwrites it until you revert it.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {EDITABLE.map((entry) => (
              <FieldEditor
                key={entry.field}
                contact={contact}
                field={entry.field}
                label={entry.label}
                kind={entry.kind}
              />
            ))}
            <MetEditor contact={contact} />
            <DoNotContactEditor contact={contact} />
          </CardContent>
        </Card>

        <div className="grid content-start gap-4">
          <Card size="sm">
            <CardHeader>
              <CardTitle>Tags</CardTitle>
            </CardHeader>
            <CardContent>
              <ContactTags contactId={contact.id} />
            </CardContent>
          </Card>

          <Card size="sm">
            <CardHeader>
              <CardTitle>Record</CardTitle>
            </CardHeader>
            <CardContent>
              <Facts
                items={[
                  ['LinkedIn URN', contact.li_urn],
                  ['LinkedIn URL', contact.li_url],
                  ['Degree', contact.degree],
                  ['First source', contact.source],
                  ['Triaged', formatDateTime(contact.triaged_at)],
                  ['Last contacted', formatDateTime(contact.last_contacted_at)],
                  ['Last enriched', formatDateTime(contact.last_enriched_at)],
                  ['Enrich priority', contact.enrich_priority],
                  ['Missing from LinkedIn', contact.li_missing_count],
                  ['Disconnected', formatDateTime(contact.li_disconnected_at)],
                  ['Archived', formatDateTime(contact.archived_at)],
                  ['Created', formatDateTime(contact.created_at)],
                  ['Updated', formatDateTime(contact.updated_at)],
                ]}
              />
            </CardContent>
          </Card>

          <Card size="sm">
            <CardHeader>
              <CardTitle>Notes</CardTitle>
              <CardDescription>Yours. No import ever touches them.</CardDescription>
            </CardHeader>
            <CardContent>
              <NotesEditor contact={contact} />
            </CardContent>
          </Card>
        </div>

        <Card size="sm">
          <CardHeader>
            <CardTitle>Contact details</CardTitle>
          </CardHeader>
          <CardContent>
            <ContactChildren contact={contact} />
          </CardContent>
        </Card>

        <div className="grid content-start gap-4">
          <Card size="sm">
            <CardHeader>
              <CardTitle>Timeline</CardTitle>
              <CardDescription>Interactions and snapshots, newest first.</CardDescription>
            </CardHeader>
            <CardContent>
              <ContactTimeline contact={contact} />
            </CardContent>
          </Card>

          <Card size="sm">
            <CardHeader>
              <CardTitle>Snapshots</CardTitle>
              <CardDescription>The headline and job as they were when seen.</CardDescription>
            </CardHeader>
            <CardContent>
              {contact.snapshots.length === 0 ? (
                <p className="text-muted-foreground">No snapshots yet.</p>
              ) : (
                <ul className="grid gap-2">
                  {contact.snapshots.map((snapshot) => (
                    <li key={snapshot.id} className="grid">
                      <span className="text-xs text-muted-foreground">
                        {formatDateTime(snapshot.observed_at)} · {snapshot.source}
                      </span>
                      <span>{snapshot.headline ?? 'no headline'}</span>
                      <span className="text-muted-foreground">
                        {[snapshot.current_title, snapshot.current_company, snapshot.location]
                          .filter(Boolean)
                          .join(' · ') || '—'}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <p className="text-xs text-muted-foreground">
        Connected on {formatDate(contact.connected_on) ?? 'an unknown date'}.
      </p>
    </div>
  )
}
