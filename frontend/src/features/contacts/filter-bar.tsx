import { useQuery } from '@tanstack/react-query'
import { Search, Tags } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import {
  Menu,
  MenuCheckboxItem,
  MenuContent,
  MenuGroupLabel,
  MenuItem,
  MenuTrigger,
} from '@/components/ui/menu'
import { Select } from '@/components/ui/select'

import { tagsQuery } from './api'
import type { ContactsSearch } from './search'
import { MET_LABELS, MET_VALUES, type ContactMet } from './types'

/**
 * How long a filter box waits after the last keystroke before it changes the
 * URL and, with it, the query.
 *
 * Typing is local to the input, so the table renders nothing per keystroke; the
 * pause is what keeps the *server* from being asked once per letter.
 */
export const FILTER_DEBOUNCE_MS = 250

function DebouncedInput({
  value,
  onCommit,
  label,
  placeholder,
  icon,
  inputRef,
}: {
  inputRef?: RefObject<HTMLInputElement | null>
  value: string
  onCommit: (next: string) => void
  label: string
  placeholder: string
  icon?: ReactNode
}) {
  const [text, setText] = useState(value)
  const [lastFromUrl, setLastFromUrl] = useState(value)
  const commit = useRef(onCommit)

  useEffect(() => {
    commit.current = onCommit
  })

  // The URL is the source of truth: the back button and an applied view both
  // arrive here as a new `value`, and the box follows. Adjusting during render
  // rather than in an effect keeps it to one pass.
  if (lastFromUrl !== value) {
    setLastFromUrl(value)
    setText(value)
  }

  useEffect(() => {
    if (text === value) return
    const timer = window.setTimeout(() => commit.current(text), FILTER_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [text, value])

  return (
    <div className="relative">
      {icon && (
        <span className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-muted-foreground">
          {icon}
        </span>
      )}
      <Input
        ref={inputRef}
        aria-label={label}
        placeholder={placeholder}
        value={text}
        className={icon ? 'w-56 pl-7' : 'w-44'}
        onChange={(event) => setText(event.target.value)}
      />
    </div>
  )
}

export interface FilterBarProps {
  search: ContactsSearch
  onChange: (patch: Partial<ContactsSearch>) => void
  /** The search box, for focus to land on once every filter is cleared. */
  searchRef?: RefObject<HTMLInputElement | null>
}

/**
 * The filters above the table. Every one of them ends up in the URL. What is
 * active, and Clear, show in the page's filter summary (#402).
 */
export function FilterBar({ search, onChange, searchRef }: FilterBarProps) {
  const tags = useQuery(tagsQuery)
  const chosenTags = new Set(search.tags ?? [])

  function toggleTag(name: string, wanted: boolean) {
    const next = new Set(chosenTags)
    if (wanted) next.add(name)
    else next.delete(name)
    onChange({ tags: next.size > 0 ? [...next] : undefined })
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <DebouncedInput
        inputRef={searchRef}
        label="Search contacts"
        placeholder="Name, company, title…"
        icon={<Search className="size-3.5" />}
        value={search.q ?? ''}
        onCommit={(q) => onChange({ q: q.trim() === '' ? undefined : q })}
      />
      <DebouncedInput
        label="Filter by company"
        placeholder="Company"
        value={search.company ?? ''}
        onCommit={(company) => onChange({ company: company.trim() === '' ? undefined : company })}
      />
      <Select
        aria-label="Filter by met"
        value={search.met ?? ''}
        onChange={(event) =>
          onChange({
            met: event.target.value === '' ? undefined : (event.target.value as ContactMet),
          })
        }
      >
        <option value="">Any met state</option>
        {MET_VALUES.map((value) => (
          <option key={value} value={value}>
            {MET_LABELS[value]}
          </option>
        ))}
      </Select>

      <Menu>
        <MenuTrigger
          render={
            <Button variant="outline" size="sm">
              <Tags data-icon="inline-start" />
              {chosenTags.size > 0 ? `${chosenTags.size} tags` : 'Tags'}
            </Button>
          }
        />
        <MenuContent align="start" className="max-h-80">
          <MenuGroupLabel>Carries any of</MenuGroupLabel>
          {tags.isPending && <MenuItem disabled>Loading tags…</MenuItem>}
          {tags.isError && <MenuItem disabled>Tags unavailable</MenuItem>}
          {tags.isSuccess && tags.data.length === 0 && <MenuItem disabled>No tags yet</MenuItem>}
          {tags.data?.map((tag) => (
            <MenuCheckboxItem
              key={tag.id}
              checked={chosenTags.has(tag.name)}
              onCheckedChange={(checked) => toggleTag(tag.name, checked === true)}
            >
              {tag.name}
              <span className="ml-auto text-xs text-muted-foreground">{tag.contact_count}</span>
            </MenuCheckboxItem>
          ))}
        </MenuContent>
      </Menu>

      <label className="flex items-center gap-1.5 text-sm">
        <Checkbox
          checked={search.dnc === true}
          onCheckedChange={(checked) => onChange({ dnc: checked === true ? true : undefined })}
          aria-label="Only do-not-contact"
        />
        Do not contact
      </label>
      <label className="flex items-center gap-1.5 text-sm">
        <Checkbox
          checked={search.archived === true}
          onCheckedChange={(checked) => onChange({ archived: checked === true ? true : undefined })}
          aria-label="Include archived"
        />
        Include archived
      </label>
    </div>
  )
}
