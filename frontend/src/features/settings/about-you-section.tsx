import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

import { type SelfContact, type SelfContactIn, saveSelfContact, selfContactQuery } from './api'

/**
 * About you (#342): your own details, held as the self contact. A test send renders a
 * template's contact fields with them, so `{{ first_name }}` there is your own first
 * name. The self contact is never in your contact lists, search, triage, exports or
 * campaigns; this is the only place to see or edit it.
 */
function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** `max` is the server's limit for the field (`SelfContactIn`), so a save never 422s. */
const FIELDS: ReadonlyArray<{
  key: keyof SelfContactIn
  label: string
  merge: string
  max: number
}> = [
  { key: 'first_name', label: 'First name', merge: 'first_name', max: 200 },
  { key: 'last_name', label: 'Last name', merge: 'last_name', max: 200 },
  { key: 'current_company', label: 'Company', merge: 'company', max: 300 },
  { key: 'current_title', label: 'Title', merge: 'title', max: 300 },
  { key: 'location', label: 'Location', merge: 'location', max: 300 },
]

export function AboutYouSection() {
  const current = useQuery(selfContactQuery)
  return (
    <Card size="sm">
      <CardHeader>
        <h2 className="font-heading text-sm leading-snug font-medium">About you</h2>
        <CardDescription>
          Your own details. A test send renders a template&apos;s contact fields with them, and goes
          only to your own mailbox. They&apos;re never in your contacts or a campaign. Templates
          have no fields about you: write your name and signature into the template itself.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {current.isPending && <p role="status">Loading…</p>}
        {current.isError && <p role="alert">{message(current.error)}</p>}
        {current.data !== undefined && <AboutYouForm current={current.data} />}
      </CardContent>
    </Card>
  )
}

function AboutYouForm({ current }: { current: SelfContact }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<SelfContactIn>({
    first_name: current.first_name,
    last_name: current.last_name,
    current_company: current.current_company,
    current_title: current.current_title,
    location: current.location,
  })
  const [saved, setSaved] = useState(false)
  const baseId = useId()
  const save = useMutation({
    mutationFn: saveSelfContact,
    onSuccess: (data) => {
      queryClient.setQueryData(selfContactQuery.queryKey, data)
      setSaved(true)
    },
  })

  return (
    <form
      aria-label="About you"
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault()
        save.mutate(draft)
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        {FIELDS.map(({ key, label, merge, max }) => (
          <div key={key} className="flex flex-col gap-1">
            <Label htmlFor={`${baseId}-${key}`}>{label}</Label>
            <Input
              id={`${baseId}-${key}`}
              value={draft[key] ?? ''}
              maxLength={max}
              aria-describedby={`${baseId}-${key}-merge`}
              onChange={(event) => {
                const value = event.target.value
                setSaved(false)
                setDraft((d) => ({ ...d, [key]: value }))
              }}
            />
            <span id={`${baseId}-${key}-merge`} className="text-xs text-muted-foreground">
              Fills <code className="font-mono">{`{{ ${merge} }}`}</code> in a test send
            </span>
          </div>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={save.isPending}>
          Save your details
        </Button>
        {saved && (
          <span role="status" className="text-muted-foreground">
            Saved.
          </span>
        )}
      </div>
      {save.isError && <p role="alert">{message(save.error)}</p>}
    </form>
  )
}
