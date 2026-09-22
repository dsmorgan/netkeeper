/**
 * The `t` key: put a tag on the contact under triage, or take one off.
 *
 * A filter box over the tag list, arrow keys to move, Enter to toggle, Escape to
 * close. Focus is in a text field the whole time, which is exactly why the
 * global key map stands aside: `m` here types an `m`.
 *
 * A name that matches no tag offers to make one. A tag somebody thinks of
 * while looking at a contact ("I met this one through that tool") is not a
 * rule and never will be, and leaving triage to go and create it loses both
 * the run and the thought. What it makes is a `manual` tag, because the
 * person is the one who said it, and the batches read that difference.
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

import { createTag, fetchTags, TriageError, type Tag, type TriageTag } from './api'

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
  const [making, setMaking] = useState(false)
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

  const wanted = query.trim()
  /** True when nothing on file is this name, so making one is the only way to it. */
  const isNew =
    wanted !== '' && !(tags ?? []).some((tag) => tag.name.toLowerCase() === wanted.toLowerCase())

  async function make() {
    if (!isNew || making) return
    setMaking(true)
    setError(null)
    try {
      const tag = await createTag(wanted)
      setTags((current) => [...(current ?? []), tag])
      onAdd({ id: tag.id, name: tag.name, color: tag.color, kind: tag.kind })
      setQuery('')
      setHighlight(0)
    } catch (failure: unknown) {
      // Somebody else's tab, or a name that normalizes onto one already there:
      // the list is stale, so take it again and apply what was already there
      // rather than telling the person their own idea is a conflict.
      if (failure instanceof TriageError && failure.status === 409) {
        const found = await fetchTags().catch(() => null)
        const existing = found?.find((tag) => tag.name.toLowerCase() === wanted.toLowerCase())
        if (found !== null) setTags(found)
        if (existing !== undefined) {
          onAdd({
            id: existing.id,
            name: existing.name,
            color: existing.color,
            kind: existing.kind,
          })
          setQuery('')
          setHighlight(0)
          setMaking(false)
          return
        }
      }
      setError(failure instanceof Error ? failure.message : 'the tag was not created')
    }
    setMaking(false)
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
              // Enter on a name nothing matches makes it, which is the whole
              // point of typing a name nothing matches.
              if (tag !== undefined) toggle(tag)
              else if (isNew) void make()
            }
          }}
        />
        <Button size="sm" variant="ghost" onClick={onClose}>
          Done
        </Button>
      </div>

      {error !== null && <p role="alert">Tags unavailable: {error}</p>}
      {tags === null && error === null && <p className="text-muted-foreground">Loading tags…</p>}
      {tags !== null && isNew && (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled={making} onClick={() => void make()}>
            {making ? 'Making…' : `Make “${wanted}” and put it on`}
          </Button>
          <span className="text-xs text-muted-foreground">
            {matches.length === 0
              ? 'No tag matches that yet.'
              : 'Or pick one of the matches below.'}
          </span>
        </div>
      )}
      {tags !== null && matches.length === 0 && !isNew && (
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
