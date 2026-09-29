import { useQuery } from '@tanstack/react-query'

import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { listsQuery, tagsQuery } from '@/features/crm/api'
import { FilterBuilder } from '@/features/crm/filter-builder'
import { emptyTree } from '@/features/crm/tree'

import type { AudienceSource } from './audience'

export function AudiencePicker({
  value,
  onChange,
  allowNone = true,
}: {
  value: AudienceSource
  onChange: (next: AudienceSource) => void
  allowNone?: boolean
}) {
  const lists = useQuery(listsQuery)
  const tags = useQuery({ ...tagsQuery, enabled: value.kind === 'filter' })

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <Label htmlFor="audience-kind">Audience from</Label>
        <Select
          id="audience-kind"
          value={value.kind}
          onChange={(event) => {
            const kind = event.target.value
            if (kind === 'list') onChange({ kind: 'list', listId: null })
            else if (kind === 'filter') onChange({ kind: 'filter', tree: emptyTree() })
            else onChange({ kind: 'none' })
          }}
          className="w-fit"
        >
          {allowNone && <option value="none">Choose later</option>}
          <option value="list">A list</option>
          <option value="filter">A filter</option>
        </Select>
      </div>
      {value.kind === 'list' && (
        <div className="flex flex-col gap-1">
          <Label htmlFor="audience-list">List</Label>
          <Select
            id="audience-list"
            value={value.listId === null ? '' : String(value.listId)}
            onChange={(event) =>
              onChange({
                kind: 'list',
                listId: event.target.value === '' ? null : Number(event.target.value),
              })
            }
            className="w-fit"
            disabled={!lists.isSuccess}
          >
            <option value="">{lists.isPending ? 'Loading lists…' : 'Pick a list'}</option>
            {(lists.data ?? []).map((list) => (
              <option key={list.id} value={list.id}>
                {list.name} ({list.member_count ?? '?'})
              </option>
            ))}
          </Select>
        </div>
      )}
      {value.kind === 'filter' && (
        <FilterBuilder
          value={value.tree}
          onChange={(tree) => onChange({ kind: 'filter', tree })}
          tags={tags.data ?? []}
        />
      )}
    </div>
  )
}
