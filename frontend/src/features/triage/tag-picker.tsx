/**
 * The `t` key: put a tag on the contact under triage, or take one off.
 *
 * A filter box over the tag list, arrow keys to move, Enter to toggle, Escape to
 * close. Focus is in a text field the whole time, which is exactly why the
 * global key map stands aside: `m` here types an `m`.
 *
 * Tagging is **not** part of the triage undo stack. It writes through the
 * contacts API (P1-07); the triage log only records decisions and preferred-name
 * edits. So `u` after `t` reaches past the tag to the decision before it, and
 * this panel says so rather than letting `u` look like it reverses the last
 * thing that happened.
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

import { fetchTags, type Tag, type TriageTag } from './api'

export function TagPicker({
  applied,
  onAdd,
  onRemove,
  onClose,
}: {
  applied: TriageTag[]
  onAdd: (tag: TriageTag) => void
  onRemove: (tagId: number) => void
  onClose: () => void
}) {
  const [tags, setTags] = useState<Tag[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [highlight, setHighlight] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const returnTo = useRef<Element | null>(null)

  useEffect(() => {
    returnTo.current = document.activeElement
    input.current?.focus()
    const target = returnTo.current
    return () => {
      if (target instanceof HTMLElement && document.contains(target)) target.focus()
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    fetchTags(controller.signal).then(
      (found) => setTags(found),
      (failure: unknown) => {
        if (controller.signal.aborted) return
        setError(failure instanceof Error ? failure.message : 'the tags could not be read')
      },
    )
    return () => controller.abort()
  }, [])

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const all = tags ?? []
    return needle === '' ? all : all.filter((tag) => tag.name.toLowerCase().includes(needle))
  }, [query, tags])

  const appliedIds = useMemo(() => new Set(applied.map((tag) => tag.id)), [applied])
  const index = Math.min(highlight, Math.max(matches.length - 1, 0))

  function toggle(tag: Tag) {
    if (appliedIds.has(tag.id)) onRemove(tag.id)
    else onAdd({ id: tag.id, name: tag.name, color: tag.color, kind: tag.kind })
  }

  return (
    <div
      aria-label="Tag this contact"
      role="group"
      className="flex flex-col gap-2 rounded-lg bg-muted/50 p-2"
    >
      <div className="flex items-center gap-2">
        <label className="text-sm" htmlFor="triage-tag-filter">
          Tag
        </label>
        <input
          ref={input}
          id="triage-tag-filter"
          className="h-8 min-w-40 flex-1 rounded-lg border border-border bg-background px-2 text-sm focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
          placeholder="Filter tags"
          value={query}
          aria-describedby="triage-tag-undo-note"
          onChange={(event) => {
            setQuery(event.target.value)
            setHighlight(0)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault()
              onClose()
              return
            }
            if (event.key === 'ArrowDown') {
              event.preventDefault()
              setHighlight((current) => Math.min(current + 1, Math.max(matches.length - 1, 0)))
              return
            }
            if (event.key === 'ArrowUp') {
              event.preventDefault()
              setHighlight((current) => Math.max(current - 1, 0))
              return
            }
            if (event.key === 'Enter') {
              event.preventDefault()
              const tag = matches[index]
              if (tag !== undefined) toggle(tag)
            }
          }}
        />
        <Button size="sm" variant="ghost" onClick={onClose}>
          Done
        </Button>
      </div>

      {error !== null && <p role="alert">Tags unavailable: {error}</p>}
      {tags === null && error === null && <p className="text-muted-foreground">Loading tags…</p>}
      {tags !== null && matches.length === 0 && (
        <p className="text-muted-foreground">No tag matches that.</p>
      )}

      <ul className="flex flex-wrap gap-1.5">
        {matches.slice(0, 24).map((tag, position) => {
          const on = appliedIds.has(tag.id)
          return (
            <li key={tag.id}>
              <Button
                size="xs"
                variant={on ? 'secondary' : 'outline'}
                aria-pressed={on}
                data-highlighted={position === index ? '' : undefined}
                className="data-highlighted:ring-3 data-highlighted:ring-ring/50"
                onClick={() => toggle(tag)}
              >
                {tag.name}
                <Badge variant="ghost">{tag.contact_count}</Badge>
              </Button>
            </li>
          )
        })}
      </ul>

      <p id="triage-tag-undo-note" className="text-xs text-muted-foreground">
        Tags are not on the triage undo stack:{' '}
        <kbd className="rounded border px-1 font-mono">u</kbd> takes back the last decision, not the
        last tag. Take a tag off by pressing it again.
      </p>
    </div>
  )
}
