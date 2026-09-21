import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Star, Trash2 } from 'lucide-react'
import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

import {
  addEmail,
  addLink,
  addPhone,
  contactsKeys,
  deleteEmail,
  deleteLink,
  deletePhone,
  makeEmailPrimary,
} from './api'
import { formatDate } from './format'
import { WriteError } from './merged-notice'
import type { ContactDetail } from './types'

/** Runs a child-row write and reloads the contact from the server afterwards. */
function useChildWrite(contactId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (run: () => Promise<void>) => run(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: contactsKeys.detail(contactId) })
      void queryClient.invalidateQueries({ queryKey: contactsKeys.pages() })
    },
  })
}

function AddRow({
  label,
  placeholder,
  onAdd,
  disabled,
}: {
  label: string
  placeholder: string
  onAdd: (value: string) => void
  disabled: boolean
}) {
  const [value, setValue] = useState('')
  function submit() {
    if (value.trim() === '') return
    onAdd(value.trim())
    setValue('')
  }
  return (
    <div className="flex items-center gap-2">
      <Input
        aria-label={label}
        placeholder={placeholder}
        value={value}
        className="w-64"
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') submit()
        }}
      />
      <Button
        size="sm"
        variant="outline"
        onClick={submit}
        disabled={disabled || value.trim() === ''}
      >
        Add
      </Button>
    </div>
  )
}

/** Addresses, numbers, links, and positions (spec 8.1). Positions come from LinkedIn only. */
export function ContactChildren({ contact }: { contact: ContactDetail }) {
  const write = useChildWrite(contact.id)
  const busy = write.isPending

  return (
    <div className="grid gap-5">
      <section className="grid gap-2">
        <h3 className="font-medium">Email addresses</h3>
        <ul className="grid gap-1">
          {contact.emails.length === 0 && <li className="text-muted-foreground">None.</li>}
          {contact.emails.map((email) => (
            <li key={email.id} className="flex items-center gap-2">
              <span className="min-w-0 truncate">{email.email}</span>
              {email.is_primary && <Badge variant="outline">Primary</Badge>}
              <Badge variant="ghost">{email.kind}</Badge>
              {email.status !== 'ok' && <Badge variant="destructive">{email.status}</Badge>}
              {!email.is_primary && (
                <Button
                  size="icon-xs"
                  variant="ghost"
                  aria-label={`Make ${email.email} primary`}
                  disabled={busy}
                  onClick={() => write.mutate(() => makeEmailPrimary(contact.id, email.id))}
                >
                  <Star />
                </Button>
              )}
              <Button
                size="icon-xs"
                variant="ghost"
                aria-label={`Remove ${email.email}`}
                disabled={busy}
                onClick={() => write.mutate(() => deleteEmail(contact.id, email.id))}
              >
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
        <AddRow
          label="New email address"
          placeholder="name@example.test"
          disabled={busy}
          onAdd={(value) => write.mutate(() => addEmail(contact.id, value))}
        />
      </section>

      <section className="grid gap-2">
        <h3 className="font-medium">Phone numbers</h3>
        <ul className="grid gap-1">
          {contact.phones.length === 0 && <li className="text-muted-foreground">None.</li>}
          {contact.phones.map((phone) => (
            <li key={phone.id} className="flex items-center gap-2">
              <span>{phone.number_e164 ?? phone.raw}</span>
              {phone.is_primary && <Badge variant="outline">Primary</Badge>}
              <Badge variant="ghost">{phone.kind}</Badge>
              <Button
                size="icon-xs"
                variant="ghost"
                aria-label={`Remove ${phone.raw}`}
                disabled={busy}
                onClick={() => write.mutate(() => deletePhone(contact.id, phone.id))}
              >
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
        <AddRow
          label="New phone number"
          placeholder="+15550100"
          disabled={busy}
          onAdd={(value) => write.mutate(() => addPhone(contact.id, value))}
        />
      </section>

      <section className="grid gap-2">
        <h3 className="font-medium">Links</h3>
        <ul className="grid gap-1">
          {contact.links.length === 0 && <li className="text-muted-foreground">None.</li>}
          {contact.links.map((link) => (
            <li key={link.id} className="flex items-center gap-2">
              <a
                href={link.url}
                target="_blank"
                rel="noreferrer noopener"
                className="min-w-0 truncate underline-offset-4 hover:underline"
              >
                {link.url}
              </a>
              <Badge variant="ghost">{link.kind}</Badge>
              <Button
                size="icon-xs"
                variant="ghost"
                aria-label={`Remove ${link.url}`}
                disabled={busy}
                onClick={() => write.mutate(() => deleteLink(contact.id, link.id))}
              >
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
        <AddRow
          label="New link"
          placeholder="https://example.test"
          disabled={busy}
          onAdd={(value) => write.mutate(() => addLink(contact.id, value))}
        />
      </section>

      <section className="grid gap-2">
        <h3 className="font-medium">Positions</h3>
        <p className="text-muted-foreground">
          Positions come from LinkedIn and the archive; they are not edited here.
        </p>
        <ul className="grid gap-1">
          {contact.positions.length === 0 && <li className="text-muted-foreground">None.</li>}
          {contact.positions.map((position) => (
            <li key={position.id} className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{position.title ?? 'Unknown title'}</span>
              <span className="text-muted-foreground">{position.company ?? 'unknown company'}</span>
              <span className="text-muted-foreground">
                {formatDate(position.started_on) ?? '?'} –{' '}
                {position.is_current ? 'now' : (formatDate(position.ended_on) ?? '?')}
              </span>
              <Badge variant="ghost">{position.source}</Badge>
            </li>
          ))}
        </ul>
      </section>

      <WriteError error={write.error} />
    </div>
  )
}
