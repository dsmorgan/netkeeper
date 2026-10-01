import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'

import {
  addListMembers,
  contactsKeys,
  contactsPageQuery,
  MAX_BULK_IDS,
  patchContact,
  setArchived,
  tagContact,
  untagContact,
  viewsQuery,
} from './api'
import { AddContactDialog } from './add-contact-dialog'
import { BulkBar } from './bulk-actions'
import { COLUMNS_BY_ID, knownColumns, type ColumnId, type ColumnSpec } from './columns'
import { ColumnPicker } from './column-picker'
import type { RowActions } from './contact-row'
import { ContactsTable } from './contacts-table'
import { FilterBar } from './filter-bar'
import {
  buildFilter,
  filterBarCanShow,
  formatSort,
  lastPage,
  pageOffset,
  pageSize,
  searchFromFilter,
  sortKeys,
  PAGE_SIZES,
  type ContactsSearch,
} from './search'
import type { BulkSelection, ContactRow, FilterTree, SavedView, SortField, SortKey } from './types'
import { useColumnPreference } from './views'
import { SavedViews } from './saved-views'

const EMPTY_ROWS: readonly ContactRow[] = []

/** The parameters that change what the server returns; touching one starts a new selection. */
const FILTER_KEYS = ['q', 'company', 'met', 'tags', 'dnc', 'archived'] as const

export interface ContactsTablePageProps {
  search: ContactsSearch
  /**
   * Rewrites the URL; the table reads its whole state back from it. `replace`
   * corrects the current entry instead of adding one, for a URL the table had to
   * fix rather than one the person asked for.
   */
  onNavigate: (
    update: (previous: ContactsSearch) => ContactsSearch,
    options?: { replace?: boolean },
  ) => void
}

export function ContactsTablePage({ search, onNavigate }: ContactsTablePageProps) {
  const queryClient = useQueryClient()
  const { columns: columnIds, setColumns } = useColumnPreference()
  const views = useQuery(viewsQuery)
  const [picked, setPicked] = useState<ReadonlySet<number>>(() => new Set())
  const [everything, setEverything] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(null)

  const columns = useMemo<ColumnSpec[]>(
    () =>
      columnIds
        .map((id) => COLUMNS_BY_ID.get(id))
        .filter((column): column is ColumnSpec => column !== undefined),
    [columnIds],
  )

  // A view's own filter wins while it is applied, because a stored tree may say
  // more than the filter bar can (P1-15's builder will say more still). Touching
  // any filter control drops `view`, and the bar takes over from there.
  const applied = views.data?.find((view) => view.id === search.view)
  // Until `/views` answers, a `?view=` link cannot be read, and querying the
  // bar's reading of it instead would show — and let you bulk-edit — a wider
  // selection than the one the link names (#88). So the page waits for it.
  const awaitingView = search.view !== undefined && !views.isSuccess
  const viewFailed = search.view !== undefined && views.isError
  const missingView = search.view !== undefined && views.isSuccess && applied === undefined
  const size = pageSize(search)
  const offset = pageOffset(search)
  const filter: FilterTree = applied?.filter ?? buildFilter(search)
  const sort = applied && applied.sort.length > 0 ? applied.sort : sortKeys(search)

  const page = useQuery({
    ...contactsPageQuery({ filter, sort, limit: size, offset }, columnIds),
    enabled: !awaitingView,
  })
  const rows = useMemo(() => page.data?.items ?? EMPTY_ROWS, [page.data])
  const total = page.data?.total ?? 0

  // A page past the last one — a hand-edited link, or rows that went away since
  // the link was made — lands on the last page rather than on an empty table
  // with Previous as the only way out.
  const pastTheEnd = page.isSuccess && offset > 0 && offset >= total
  const finalPage = lastPage(total, size)
  useEffect(() => {
    if (!pastTheEnd) return
    onNavigate(
      (previous) => {
        const next = { ...previous }
        if (finalPage > 1) next.page = finalPage
        else delete next.page
        return next
      },
      { replace: true },
    )
  }, [pastTheEnd, finalPage, onNavigate])

  const update = useCallback(
    (patch: Partial<ContactsSearch>) => {
      onNavigate((previous) => {
        const next: ContactsSearch = { ...previous, ...patch }
        // Any change but paging starts at page one again.
        if (!('page' in patch)) delete next.page
        // Touching a filter means this is no longer the saved view it came from.
        if (FILTER_KEYS.some((key) => key in patch)) delete next.view
        for (const key of Object.keys(next) as (keyof ContactsSearch)[]) {
          if (next[key] === undefined) delete next[key]
        }
        return next
      })
    },
    [onNavigate],
  )

  // A new filter selects nothing: the rows behind the old selection are gone.
  // Adjusting during render, not in an effect, so no pass ever shows a bulk bar
  // counting rows the filter no longer matches.
  const selectionScope = JSON.stringify([
    search.q,
    search.company,
    search.met,
    search.tags,
    search.dnc,
    search.archived,
    search.view,
  ])
  const [lastScope, setLastScope] = useState(selectionScope)
  if (lastScope !== selectionScope) {
    setLastScope(selectionScope)
    setPicked(new Set())
    setEverything(false)
  }

  const rowAction = useMutation({
    mutationFn: (run: () => Promise<unknown>) => run(),
    onSuccess: () => {
      setFailure(null)
      void queryClient.invalidateQueries({ queryKey: contactsKeys.all })
      // A row action can change a tag's count or a list's membership.
      void queryClient.invalidateQueries({ queryKey: ['tags'] })
      void queryClient.invalidateQueries({ queryKey: ['lists'] })
    },
    onError: (error: Error) => setFailure(error.message),
  })
  const runRowAction = rowAction.mutate

  const actions = useMemo<RowActions>(
    () => ({
      setMet: (row, met) => runRowAction(() => patchContact(row.id, { met })),
      setDoNotContact: (row, value) =>
        runRowAction(() => patchContact(row.id, { do_not_contact: value })),
      setArchived: (row, archived) => runRowAction(() => setArchived(row.id, archived)),
      toggleTag: (row, tagId, wanted) =>
        runRowAction(() => (wanted ? tagContact(row.id, tagId) : untagContact(row.id, tagId))),
      addToList: (row, listId) => runRowAction(() => addListMembers(listId, [row.id])),
    }),
    [runRowAction],
  )

  const onSelect = useCallback((id: number, selected: boolean) => {
    setEverything(false)
    setPicked((current) => {
      // Past the cap a pick is refused here; the bulk bar says why.
      if (selected && !current.has(id) && current.size >= MAX_BULK_IDS) return current
      const next = new Set(current)
      if (selected) next.add(id)
      else next.delete(id)
      return next
    })
  }, [])

  const onSelectPage = useCallback(
    (selected: boolean) => {
      setEverything(false)
      setPicked(selected ? new Set(rows.map((row) => row.id)) : new Set())
    },
    [rows],
  )

  const onSort = useCallback(
    (field: SortField) => {
      const current = sortKeys(search)
      const existing = current.find((key) => key.field === field)
      const next: SortKey[] =
        existing === undefined
          ? [{ field, direction: 'asc' }]
          : existing.direction === 'asc'
            ? [{ field, direction: 'desc' }]
            : []
      update({ sort: next.length > 0 ? formatSort(next) : undefined })
    },
    [search, update],
  )

  const applyView = useCallback(
    (view: SavedView) => {
      setColumns(knownColumns(view.columns))
      // The readable part of the filter goes into the bar so it can be edited;
      // `view` keeps the stored tree in force until it is.
      onNavigate(() => ({ ...searchFromFilter(view.filter, view.sort), view: view.id }))
    },
    [onNavigate, setColumns],
  )

  // A bulk action applies to the filter the table is showing, whatever put it
  // there: the bar, or an applied view's own tree.
  const selection: BulkSelection = everything
    ? { filter, ids: null }
    : { filter: null, ids: [...picked] }
  const showBulk = everything || picked.size > 0
  const first = total === 0 ? 0 : offset + 1
  const last = Math.min(offset + rows.length, total)

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <FilterBar
          search={search}
          onChange={update}
          onClear={() =>
            update({
              q: undefined,
              company: undefined,
              met: undefined,
              tags: undefined,
              dnc: undefined,
              archived: undefined,
              view: undefined,
            })
          }
        />
        <div className="flex items-center gap-2">
          <SavedViews
            current={search}
            filter={filter}
            sort={sort}
            columns={columnIds}
            activeId={search.view}
            onApply={applyView}
          />
          <ColumnPicker
            columns={columnIds}
            onChange={(ids: readonly ColumnId[]) => setColumns(ids)}
          />
          <AddContactDialog />
        </div>
      </div>

      {viewFailed && (
        <div role="alert" className="grid gap-2 rounded-xl bg-destructive/10 p-4 text-destructive">
          <p>
            The saved view in this link could not be loaded, so its contacts are not shown:{' '}
            {views.error?.message}
          </p>
          <div>
            <Button size="sm" variant="outline" onClick={() => void views.refetch()}>
              Try again
            </Button>
          </div>
        </div>
      )}

      {missingView && (
        <p role="status" className="rounded-lg bg-muted/60 px-3 py-2">
          Saved view {search.view} no longer exists. The table shows the filter the link carried.
        </p>
      )}

      {applied !== undefined && !filterBarCanShow(applied.filter) && (
        <p role="note" className="rounded-lg bg-amber-500/10 px-3 py-2">
          “{applied.name}” filters on more than the controls above can show. Changing any of them
          replaces the view’s filter with only what they show, which may match more contacts.
        </p>
      )}

      {failure && (
        <p role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-destructive">
          {failure}
        </p>
      )}

      {status && (
        <p role="status" className="rounded-lg bg-muted/60 px-3 py-2">
          {status}
        </p>
      )}

      {showBulk && (
        <BulkBar
          selection={selection}
          selectedCount={everything ? total : picked.size}
          pickLimit={MAX_BULK_IDS}
          everything={everything}
          total={total}
          onSelectEverything={() => setEverything(true)}
          onClear={() => {
            setEverything(false)
            setPicked(new Set())
          }}
          onApplied={(affected) => {
            setEverything(false)
            setPicked(new Set())
            setFailure(null)
            void queryClient.invalidateQueries({ queryKey: contactsKeys.all })
            setStatus(
              `${affected.toLocaleString()} ${affected === 1 ? 'contact' : 'contacts'} updated.`,
            )
          }}
        />
      )}

      {page.isPending && !viewFailed && <p className="text-muted-foreground">Loading contacts…</p>}

      {page.isError && (
        <div role="alert" className="grid gap-2 rounded-xl bg-destructive/10 p-4 text-destructive">
          <p>Contacts could not be loaded: {page.error.message}</p>
          <div>
            <Button size="sm" variant="outline" onClick={() => void page.refetch()}>
              Try again
            </Button>
          </div>
        </div>
      )}

      {page.isSuccess && rows.length === 0 && (
        <div className="grid justify-items-start gap-2 rounded-xl bg-muted/40 p-6">
          <p className="font-medium">No contacts match this filter.</p>
          <p className="text-muted-foreground">{page.data.describe}</p>
        </div>
      )}

      {page.isSuccess && rows.length > 0 && (
        <ContactsTable
          rows={rows}
          scrollKey={JSON.stringify([filter, sort, size, offset])}
          columns={columns}
          sort={sort}
          onSort={onSort}
          selectedIds={picked}
          everything={everything}
          onSelect={onSelect}
          onSelectPage={onSelectPage}
          actions={actions}
        />
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
        <span role="status" aria-label="Contacts shown">
          {page.isSuccess
            ? `${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()} · ${page.data.describe}`
            : ' '}
        </span>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1.5">
            Rows
            <Select
              aria-label="Rows per page"
              value={String(size)}
              onChange={(event) => update({ size: Number(event.target.value), page: 1 })}
            >
              {PAGE_SIZES.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </Select>
          </label>
          <Button
            variant="outline"
            size="sm"
            disabled={offset === 0}
            onClick={() => update({ page: Math.max(1, (search.page ?? 1) - 1) })}
          >
            <ChevronLeft data-icon="inline-start" />
            Previous
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={offset + rows.length >= total}
            onClick={() => update({ page: (search.page ?? 1) + 1 })}
          >
            Next
            <ChevronRight data-icon="inline-end" />
          </Button>
        </div>
      </div>
    </div>
  )
}
