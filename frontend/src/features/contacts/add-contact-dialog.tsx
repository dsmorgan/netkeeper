/**
 * "Add contact": one contact by hand, from the Contacts page (#303).
 *
 * The form checks what it can before anything is sent (a name, an address that
 * looks like one, a LinkedIn profile URL), and the backend checks again with
 * the importer's own rules, answering per field. Someone already in the address
 * book is never added twice: the backend answers with the contact it found, and
 * the form offers to open it. A match on name and company alone is only a
 * likely one, so there the form also offers "Add anyway".
 *
 * On success the dialog closes and the new contact's page opens.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from '@tanstack/react-router'
import { UserPlus } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'

import {
  contactsKeys,
  createContact,
  fieldErrors,
  listsQuery,
  tagsQuery,
  type CreateContactResult,
} from './api'
import type { ContactCreate, DuplicateContact } from './types'

type TextField =
  'first_name' | 'last_name' | 'email' | 'current_company' | 'current_title' | 'li_url'

const TEXT_FIELDS: ReadonlyArray<{
  name: TextField
  label: string
  type?: string
  placeholder?: string
}> = [
  { name: 'first_name', label: 'First name' },
  { name: 'last_name', label: 'Last name' },
  { name: 'email', label: 'Email', type: 'email', placeholder: 'name@example.com' },
  { name: 'current_company', label: 'Company' },
  { name: 'current_title', label: 'Title' },
  {
    name: 'li_url',
    label: 'LinkedIn URL',
    type: 'url',
    placeholder: 'https://www.linkedin.com/in/…',
  },
]

type Draft = Record<TextField, string>

const EMPTY: Draft = {
  first_name: '',
  last_name: '',
  email: '',
  current_company: '',
  current_title: '',
  li_url: '',
}

/** The importer's loose check (`is_email_address`): one @, a dot in the domain, no spaces. */
const EMAIL = /^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$/
/**
 * A profile URL on linkedin.com or a country subdomain, with or without the scheme
 * or a port: what the backend's `public_id_from_url` reads, which ignores the port.
 */
const LINKEDIN_PROFILE =
  /^(https?:\/\/)?([a-z0-9-]+\.)*linkedin\.com(:\d+)?\/in\/[^/?#\s]+\/?([?#]\S*)?$/i

const MATCHED_BY: Record<DuplicateContact['matched_by'], string> = {
  email: 'that email address',
  linkedin: 'that LinkedIn profile',
  name: 'the same name and company',
}

/** What the form can say before anything is sent. The backend checks all of it again. */
function validateDraft(draft: Draft): Partial<Record<TextField, string>> {
  const errors: Partial<Record<TextField, string>> = {}
  if (draft.first_name.trim() === '' && draft.last_name.trim() === '') {
    errors.first_name = 'Enter a first name or a last name.'
  }
  const email = draft.email.trim()
  if (email !== '' && !EMAIL.test(email)) {
    errors.email = 'Enter one email address, such as name@example.com.'
  }
  const url = draft.li_url.trim()
  if (url !== '' && !LINKEDIN_PROFILE.test(url)) {
    errors.li_url = 'Enter a LinkedIn profile URL, such as https://www.linkedin.com/in/name.'
  }
  return errors
}

function toBody(
  draft: Draft,
  tagIds: ReadonlySet<number>,
  listId: number | null,
  allowNameMatch: boolean,
): ContactCreate {
  const text = (value: string) => (value.trim() === '' ? null : value.trim())
  return {
    first_name: text(draft.first_name),
    last_name: text(draft.last_name),
    email: text(draft.email),
    current_company: text(draft.current_company),
    current_title: text(draft.current_title),
    li_url: text(draft.li_url),
    tag_ids: [...tagIds],
    list_id: listId,
    allow_name_match: allowNameMatch,
  }
}

export function AddContactDialog() {
  const [open, setOpen] = useState(false)
  // A fresh form each time the dialog opens: the key remounts it.
  const [session, setSession] = useState(0)
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (next) setSession((current) => current + 1)
      }}
    >
      <DialogTrigger
        render={
          <Button>
            <UserPlus data-icon="inline-start" aria-hidden />
            Add contact
          </Button>
        }
      />
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add a contact</DialogTitle>
          <DialogDescription>
            A first or a last name is enough. Someone already in your contacts, by email or LinkedIn
            URL, is never added twice.
          </DialogDescription>
        </DialogHeader>
        <AddContactForm key={session} onDone={() => setOpen(false)} />
      </DialogContent>
    </Dialog>
  )
}

/** The form itself, exported on its own for tests. `onDone` runs once the contact exists. */
export function AddContactForm({ onDone }: { onDone: () => void }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const tags = useQuery(tagsQuery)
  const lists = useQuery(listsQuery)
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [tagIds, setTagIds] = useState<ReadonlySet<number>>(() => new Set())
  const [listId, setListId] = useState<number | null>(null)
  const [errors, setErrors] = useState<Partial<Record<string, string>>>({})
  const [duplicate, setDuplicate] = useState<DuplicateContact | null>(null)
  // A refusal that names no field (a failure that is not a 422, say) is said on its own.
  const [failure, setFailure] = useState<string | null>(null)

  const staticLists = (lists.data ?? []).filter((list) => list.kind === 'static')

  const add = useMutation({
    mutationFn: (allowNameMatch: boolean) =>
      createContact(toBody(draft, tagIds, listId, allowNameMatch)),
    onSuccess: (result: CreateContactResult) => {
      if (result.kind === 'duplicate') {
        setDuplicate(result.duplicate)
        return
      }
      void queryClient.invalidateQueries({ queryKey: contactsKeys.all })
      void queryClient.invalidateQueries({ queryKey: ['tags'] })
      void queryClient.invalidateQueries({ queryKey: ['lists'] })
      onDone()
      void navigate({
        to: '/contacts/$contactId',
        params: { contactId: String(result.contact.id) },
      })
    },
    onError: (error: Error) => {
      const found = fieldErrors(error)
      setErrors(found)
      setFailure(Object.keys(found).length > 0 ? null : error.message)
    },
  })

  const submit = (allowNameMatch: boolean) => {
    const found = validateDraft(draft)
    setErrors(found)
    setDuplicate(null)
    setFailure(null)
    if (Object.keys(found).length > 0) return
    add.mutate(allowNameMatch)
  }

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    submit(false)
  }

  const edit = (name: TextField, value: string) => {
    setDraft((current) => ({ ...current, [name]: value }))
    // What the backend said was about the values it was sent.
    setDuplicate(null)
    setErrors((current) => ({ ...current, [name]: undefined }))
  }

  /** A tag or the list changed: what the backend said about the old choice no longer holds. */
  const chose = () => {
    setDuplicate(null)
    setErrors((current) => ({ ...current, tag_ids: undefined, list_id: undefined }))
  }

  return (
    <form noValidate onSubmit={onSubmit} className="grid gap-3" aria-label="Add a contact">
      <div className="grid gap-3 sm:grid-cols-2">
        {TEXT_FIELDS.map((field) => {
          const id = `add-contact-${field.name}`
          const problem = errors[field.name]
          return (
            <div
              key={field.name}
              className={
                field.name === 'li_url' || field.name === 'email'
                  ? 'grid gap-1 sm:col-span-2'
                  : 'grid gap-1'
              }
            >
              <Label htmlFor={id}>{field.label}</Label>
              <Input
                id={id}
                name={field.name}
                type={field.type ?? 'text'}
                placeholder={field.placeholder}
                value={draft[field.name]}
                onChange={(event) => edit(field.name, event.target.value)}
                aria-invalid={problem !== undefined}
                aria-describedby={problem !== undefined ? `${id}-error` : undefined}
                autoFocus={field.name === 'first_name'}
              />
              {problem !== undefined && (
                <p id={`${id}-error`} className="text-xs text-destructive">
                  {problem}
                </p>
              )}
            </div>
          )
        })}
      </div>

      {tags.isSuccess && tags.data.length > 0 && (
        <fieldset
          className="grid gap-1.5"
          aria-describedby={errors.tag_ids !== undefined ? 'add-contact-tags-error' : undefined}
        >
          <legend className="mb-1 text-sm font-medium">Tags</legend>
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            {tags.data.map((tag) => (
              <Label key={tag.id} className="font-normal">
                <Checkbox
                  checked={tagIds.has(tag.id)}
                  onCheckedChange={(checked) => {
                    chose()
                    setTagIds((current) => {
                      const next = new Set(current)
                      if (checked === true) next.add(tag.id)
                      else next.delete(tag.id)
                      return next
                    })
                  }}
                />
                {tag.name}
              </Label>
            ))}
          </div>
          {errors.tag_ids !== undefined && (
            <p id="add-contact-tags-error" className="text-xs text-destructive">
              {errors.tag_ids}
            </p>
          )}
        </fieldset>
      )}

      {staticLists.length > 0 && (
        <div className="grid gap-1">
          <Label htmlFor="add-contact-list">Add to list</Label>
          <Select
            id="add-contact-list"
            className="w-full"
            value={listId === null ? '' : String(listId)}
            aria-invalid={errors.list_id !== undefined}
            aria-describedby={errors.list_id !== undefined ? 'add-contact-list-error' : undefined}
            onChange={(event) => {
              chose()
              setListId(event.target.value === '' ? null : Number(event.target.value))
            }}
          >
            <option value="">No list</option>
            {staticLists.map((list) => (
              <option key={list.id} value={list.id}>
                {list.name}
              </option>
            ))}
          </Select>
          {errors.list_id !== undefined && (
            <p id="add-contact-list-error" className="text-xs text-destructive">
              {errors.list_id}
            </p>
          )}
        </div>
      )}

      {duplicate !== null && (
        <div role="alert" className="grid gap-2 rounded-lg bg-amber-500/10 px-3 py-2">
          <p>
            {duplicate.matched_by === 'name'
              ? 'This may already be a contact'
              : 'Already a contact'}
            : contact {duplicate.contact_id} has {MATCHED_BY[duplicate.matched_by]}
            {duplicate.archived ? ' and is archived' : ''}.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <Link
              to="/contacts/$contactId"
              params={{ contactId: String(duplicate.contact_id) }}
              onClick={onDone}
              className="underline underline-offset-4"
            >
              Open contact {duplicate.contact_id}
            </Link>
            {duplicate.matched_by === 'name' && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={add.isPending}
                onClick={() => submit(true)}
              >
                Add anyway
              </Button>
            )}
          </div>
        </div>
      )}

      {failure !== null && (
        <p role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-destructive">
          {failure}
        </p>
      )}

      <DialogFooter>
        <DialogClose render={<Button variant="ghost">Cancel</Button>} />
        <Button type="submit" disabled={add.isPending}>
          {add.isPending ? 'Adding…' : 'Add contact'}
        </Button>
      </DialogFooter>
    </form>
  )
}
