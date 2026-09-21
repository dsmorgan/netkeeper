/**
 * The columns this browser is showing.
 *
 * Saved views live on the server (P1-08's `/views`); this is the looser thing
 * beside them — the columns you are looking at right now, remembered so a
 * reload does not undo them. Every read and write is guarded: a browser with
 * storage blocked still gets a working table with the default columns.
 */

import { useCallback, useState } from 'react'

import type { ColumnId } from './columns'
import { DEFAULT_COLUMNS, knownColumns } from './columns'
const COLUMNS_KEY = 'netkeeper.contacts.columns'

function read(key: string): unknown {
  try {
    const raw = window.localStorage.getItem(key)
    return raw === null ? null : JSON.parse(raw)
  } catch {
    return null
  }
}

function write(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Storage blocked or full: the preference is lost, the table is not.
  }
}

export function loadColumns(): ColumnId[] {
  const stored = read(COLUMNS_KEY)
  if (!Array.isArray(stored)) return [...DEFAULT_COLUMNS]
  const ids = knownColumns(stored.map(String))
  return ids.length > 0 ? ids : [...DEFAULT_COLUMNS]
}

/** The chosen columns, in the order the registry lists them, kept in local storage. */
export function useColumnPreference(): {
  columns: ColumnId[]
  setColumns: (ids: readonly ColumnId[]) => void
} {
  const [columns, setState] = useState<ColumnId[]>(loadColumns)

  const setColumns = useCallback((ids: readonly ColumnId[]) => {
    // A table with no columns is unusable, so the last one cannot be turned off.
    const next = ids.length > 0 ? [...ids] : [...DEFAULT_COLUMNS]
    setState(next)
    write(COLUMNS_KEY, next)
  }, [])

  return { columns, setColumns }
}
