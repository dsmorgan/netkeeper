import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bookmark, Trash2 } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  Menu,
  MenuContent,
  MenuGroupLabel,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
} from '@/components/ui/menu'

import { createView, deleteView, viewsQuery } from './api'
import type { ColumnId } from './columns'
import { describeSearch } from './describe'
import type { ContactsSearch } from './search'
import type { FilterTree, SavedView, SortKey } from './types'

/**
 * Saved views: a column set, a sort, and a filter under a name (spec 10.1).
 *
 * They live on the server (P1-08's `/views`), not in this browser, so the same
 * views are there on the next machine. Applying one navigates, so the URL still
 * says what is on screen.
 */
export function SavedViews({
  current,
  filter,
  sort,
  columns,
  activeId,
  onApply,
}: {
  /** The search parameters, for reading the filter back in words. */
  current: ContactsSearch
  /**
   * The filter the table is running, which is not always what the filter bar
   * says: an applied view's stored tree may carry predicates no control here
   * can show. Saving reuses this, so what you save is what you are looking at.
   */
  filter: FilterTree
  sort: readonly SortKey[]
  columns: readonly ColumnId[]
  activeId: number | undefined
  onApply: (view: SavedView) => void
}) {
  const queryClient = useQueryClient()
  const views = useQuery(viewsQuery)
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')

  const save = useMutation({
    mutationFn: (viewName: string) =>
      createView({
        name: viewName,
        columns: [...columns],
        sort: [...sort],
        filter,
      }),
    onSuccess: (view) => {
      void queryClient.invalidateQueries({ queryKey: viewsQuery.queryKey })
      setNaming(false)
      setName('')
      onApply(view)
    },
  })

  const remove = useMutation({
    mutationFn: (viewId: number) => deleteView(viewId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: viewsQuery.queryKey }),
  })

  const active = views.data?.find((view) => view.id === activeId)

  return (
    <>
      <Menu>
        <MenuTrigger
          render={
            <Button variant="outline" size="sm">
              <Bookmark data-icon="inline-start" />
              {active?.name ?? 'Views'}
            </Button>
          }
        />
        <MenuContent align="start" className="min-w-56">
          <MenuGroupLabel>Saved views</MenuGroupLabel>
          {views.isPending && <MenuItem disabled>Loading views…</MenuItem>}
          {views.isError && <MenuItem disabled>Views unavailable</MenuItem>}
          {views.isSuccess && views.data.length === 0 && (
            <MenuItem disabled>No saved views yet</MenuItem>
          )}
          {views.data?.map((view) => (
            <MenuItem key={view.id} onClick={() => onApply(view)} className="justify-between gap-4">
              <span className="truncate">{view.name}</span>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={`Delete view ${view.name}`}
                disabled={remove.isPending}
                onClick={(event) => {
                  event.stopPropagation()
                  remove.mutate(view.id)
                }}
              >
                <Trash2 />
              </Button>
            </MenuItem>
          ))}
          <MenuSeparator />
          <MenuItem
            onClick={() => {
              setName('')
              setNaming(true)
            }}
          >
            Save this view…
          </MenuItem>
        </MenuContent>
      </Menu>

      <Dialog open={naming} onOpenChange={setNaming}>
        <DialogContent aria-label="Save this view">
          <DialogTitle>Save this view</DialogTitle>
          <DialogDescription>
            {describeSearch(current)}, with the {columns.length} columns on screen.
          </DialogDescription>
          <label className="grid gap-1.5">
            <span className="text-muted-foreground">Name</span>
            <Input
              value={name}
              autoFocus
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && name.trim() !== '') save.mutate(name.trim())
              }}
              placeholder="People I met in March"
            />
          </label>
          {save.isError && (
            <p role="alert" className="text-destructive">
              {save.error.message}
            </p>
          )}
          <DialogFooter>
            <DialogClose render={<Button variant="ghost" size="sm" />}>Cancel</DialogClose>
            <Button
              size="sm"
              onClick={() => save.mutate(name.trim())}
              disabled={name.trim() === '' || save.isPending}
            >
              Save view
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
