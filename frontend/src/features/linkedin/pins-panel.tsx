import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { contactsPageQuery } from '@/features/contacts/api'
import { buildFilter, DEFAULT_SORT } from '@/features/contacts/search'
import { INPUT_CLASS } from '@/features/imports/styles'
import { cn } from '@/lib/utils'

import { linkedinKeys, pinContact, pinsQuery, unpinContact } from './api'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const MAX_PINS = 5

/**
 * Up to 5 contacts pinned to the front of the next enrichment run (spec 9.6):
 * a pin overrides the usual priority order but never spends outside the
 * budget, and it is dropped once a run finishes with that contact, pinned or
 * not.
 */
export function PinsPanel() {
  const pins = useQuery(pinsQuery)
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')

  const pin = useMutation({
    mutationFn: pinContact,
    onSuccess: (data) => {
      queryClient.setQueryData(linkedinKeys.pins(), data)
      setSearch('')
    },
  })
  const unpin = useMutation({
    mutationFn: unpinContact,
    onSuccess: (data) => queryClient.setQueryData(linkedinKeys.pins(), data),
  })

  const results = useQuery({
    ...contactsPageQuery(
      { filter: buildFilter({ q: search }), sort: DEFAULT_SORT, limit: 6, offset: 0 },
      [],
    ),
    enabled: search.trim().length >= 2,
  })

  const pinnedIds = new Set((pins.data ?? []).map((row) => row.contact_id))
  const atMax = pinnedIds.size >= MAX_PINS

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Pins</CardTitle>
        <CardDescription>
          At the front of the next enrichment run, up to {MAX_PINS} at a time.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {pins.isPending && <p role="status">Loading…</p>}
        {pins.isError && <p role="alert">{message(pins.error)}</p>}
        {pins.isSuccess && (
          <>
            <p className="text-muted-foreground">
              {pinnedIds.size} of {MAX_PINS} pinned
            </p>
            {pins.data.length > 0 && (
              <ul className="space-y-1">
                {pins.data.map((row) => (
                  <li key={row.contact_id} className="flex items-center justify-between gap-2">
                    <span>
                      {row.first_name} {row.last_name}
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => unpin.mutate(row.contact_id)}
                      disabled={unpin.isPending}
                      aria-label={`Unpin ${row.first_name} ${row.last_name}`}
                    >
                      Unpin
                    </Button>
                  </li>
                ))}
              </ul>
            )}

            {pin.isError && <p role="alert">{message(pin.error)}</p>}
            {unpin.isError && <p role="alert">{message(unpin.error)}</p>}

            {atMax ? (
              <p className="text-muted-foreground">
                {MAX_PINS} are already pinned; unpin one first.
              </p>
            ) : (
              <div className="space-y-2">
                <label htmlFor="pin-search" className="text-xs text-muted-foreground">
                  Add a contact
                </label>
                <input
                  id="pin-search"
                  type="text"
                  className={cn(INPUT_CLASS, 'w-full')}
                  placeholder="Search by name…"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
                {results.isSuccess && search.trim().length >= 2 && (
                  <ul className="space-y-1">
                    {results.data.items.length === 0 && (
                      <li className="text-muted-foreground">No match.</li>
                    )}
                    {results.data.items
                      .filter((row) => !pinnedIds.has(row.id))
                      .map((row) => (
                        <li key={row.id} className="flex items-center justify-between gap-2">
                          <span>
                            {row.first_name} {row.last_name}
                          </span>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => pin.mutate(row.id)}
                            disabled={pin.isPending}
                          >
                            Pin
                          </Button>
                        </li>
                      ))}
                  </ul>
                )}
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
