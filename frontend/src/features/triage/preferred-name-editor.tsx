/**
 * The `p` key: what you call this person.
 *
 * An input, not a dialog. It appears under the card, takes focus so the next
 * keystroke is a letter rather than a decision, and gives focus back when it
 * closes. The global map stands aside on its own while a text field has focus,
 * so nothing has to be disabled here.
 *
 * Enter saves, Escape cancels. An empty value is meaningful: the backend reads
 * it as "use the first name" and answers with what it stored.
 */

import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'

export function PreferredNameEditor({
  contactId,
  initial,
  firstName,
  onSave,
  onClose,
}: {
  contactId: number
  initial: string
  firstName: string
  onSave: (contactId: number, preferredName: string) => Promise<void>
  onClose: () => void
}) {
  const [value, setValue] = useState(initial)
  const input = useRef<HTMLInputElement>(null)
  const returnTo = useRef<Element | null>(null)

  useEffect(() => {
    returnTo.current = document.activeElement
    input.current?.focus()
    input.current?.select()
    const target = returnTo.current
    return () => {
      if (target instanceof HTMLElement && document.contains(target)) target.focus()
    }
  }, [])

  function save() {
    void onSave(contactId, value.trim())
    onClose()
  }

  return (
    <form
      aria-label="Edit the preferred name"
      className="flex flex-wrap items-center gap-2 rounded-lg bg-muted/50 p-2"
      onSubmit={(event) => {
        event.preventDefault()
        save()
      }}
    >
      <label className="text-sm" htmlFor="triage-preferred-name">
        Preferred name
      </label>
      <input
        ref={input}
        id="triage-preferred-name"
        className="h-8 min-w-40 flex-1 rounded-lg border border-border bg-background px-2 text-sm focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
        value={value}
        placeholder={firstName}
        maxLength={200}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.preventDefault()
            onClose()
          }
        }}
      />
      <Button size="sm" type="submit">
        Save
      </Button>
      <Button size="sm" variant="ghost" type="button" onClick={onClose}>
        Cancel
      </Button>
      <p className="w-full text-xs text-muted-foreground">
        Empty falls back to {firstName}. This is a manual override no later sync overwrites, and
        <kbd className="mx-1 rounded border px-1 font-mono">u</kbd>
        takes it back.
      </p>
    </form>
  )
}
