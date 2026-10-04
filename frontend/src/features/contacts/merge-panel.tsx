/**
 * Merge two contacts from the UI (#363).
 *
 * Pick the other contact by name or email, see side by side what survives, and
 * confirm. The "after" column is the backend's own merge, run in a savepoint
 * and rolled back (`POST /contacts/{id}/merge/preview`), so it shows every
 * merge rule as the merge applies it: who decided Met (#331), a card's
 * headline (#186), a reply's review mark (#65), and campaign rows (#242). This
 * screen never works a rule out for itself.
 *
 * A merge is destructive and nothing takes one back, so it goes through a
 * `ConfirmDialog` that lists what moves and says so. The dialog takes the
 * merge's promise (#364), so a double click sends one merge.
 *
 * The same panel serves the contact page's **Merge with…** and the
 * possible-duplicate hint on a review band, there and on the triage screen.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeftRight } from 'lucide-react'
import type { ReactNode } from 'react'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useDebounced } from '@/features/crm/use-debounced'

import {
  MERGE_PICKER_LIMIT,
  contactsKeys,
  mergeContacts,
  mergePreviewQuery,
  searchMergeCandidates,
  type MergeCandidate,
} from './api'
import { displayName, formatDateTime } from './format'
import { describeMoves, labelled, labelledDetail } from './merge-text'
import type { ContactDetail, MergeMoves } from './types'
import { MET_LABELS } from './types'

/**
 * A contact in the merge, once picked: its id and its name with a distinguishing
 * detail ({@link labelled}), which names it before its preview arrives.
 */
export interface MergeTarget {
  id: number
  name: string
}

/** Which of the two keeps its id: this page's contact, or the other one. */
export type Survivor = 'this' | 'other'

export function MergePanel({
  contact,
  initialTarget = null,
  initialSurvivor = 'this',
  onClose,
  onMerged,
}: {
  /** The contact the panel was opened from. */
  contact: MergeTarget
  initialTarget?: MergeTarget | null
  initialSurvivor?: Survivor
  onClose: () => void
  /** The merge landed: the survivor as the server answered, and the id merged away. */
  onMerged: (survivor: ContactDetail, loserId: number) => void
}) {
  const [target, setTarget] = useState<MergeTarget | null>(initialTarget)
  const [survivor, setSurvivor] = useState<Survivor>(initialSurvivor)

  return (
    <section
      aria-label="Merge contacts"
      data-testid="merge-panel"
      className="flex flex-col gap-3 rounded-lg bg-card p-4 text-sm ring-1 ring-foreground/10"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-heading text-base font-medium">
          Merge {contact.name} with another contact
        </h3>
        <Button size="sm" variant="ghost" className="ml-auto" onClick={onClose}>
          Close
        </Button>
      </div>
      {target === null ? (
        <MergePicker
          exclude={contact.id}
          onPick={(picked) => {
            setTarget(picked)
            setSurvivor('this')
          }}
        />
      ) : (
        <MergeReview
          keep={survivor === 'this' ? contact : target}
          fold={survivor === 'this' ? target : contact}
          onSwap={() => setSurvivor((side) => (side === 'this' ? 'other' : 'this'))}
          onPickAnother={() => setTarget(null)}
          onMerged={onMerged}
        />
      )}
    </section>
  )
}

/** Search by first, last or preferred name, or email, and pick one contact. */
function MergePicker({
  exclude,
  onPick,
}: {
  exclude: number
  onPick: (target: MergeTarget) => void
}) {
  const inputId = useId()
  const [text, setText] = useState('')
  const settled = useDebounced(text.trim(), 200)
  const matches = useQuery({
    queryKey: [...contactsKeys.all, 'merge-picker', settled],
    queryFn: ({ signal }) => searchMergeCandidates(settled, signal),
    enabled: settled !== '',
    gcTime: 0,
    retry: false,
  })
  const items = (matches.data?.items ?? []).filter((row) => row.id !== exclude)

  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={inputId}>Find the other contact</Label>
      <Input
        id={inputId}
        type="search"
        value={text}
        autoFocus
        placeholder="Name or email address"
        onChange={(event) => setText(event.target.value)}
      />
      {settled !== '' && matches.isError && (
        <p role="alert" className="text-destructive">
          The search failed: {matches.error.message}
        </p>
      )}
      {settled !== '' && matches.isSuccess && items.length === 0 && (
        <p className="text-muted-foreground">Nobody else matches “{settled}”.</p>
      )}
      {items.length > 0 && (
        <ul aria-label="Matches" className="divide-y rounded-lg border">
          {items.map((row) => (
            <li key={row.id}>
              <button
                type="button"
                className="flex w-full flex-col items-start px-3 py-1.5 text-left hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
                onClick={() => onPick({ id: row.id, name: labelled(row) })}
              >
                <span className="font-medium">
                  {displayName(row)}
                  {row.archived && <span className="text-muted-foreground"> · Archived</span>}
                </span>
                <CandidateDetail row={row} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {matches.isSuccess && matches.data.total > MERGE_PICKER_LIMIT && (
        <p className="text-xs text-muted-foreground">
          Showing {MERGE_PICKER_LIMIT} of {matches.data.total}. Type more to narrow it.
        </p>
      )}
    </div>
  )
}

function CandidateDetail({ row }: { row: MergeCandidate }) {
  const detail = [row.current_title, row.current_company, row.primary_email]
    .filter((part) => part !== null && part !== '')
    .join(' · ')
  if (detail === '') return null
  return <span className="text-xs text-muted-foreground">{detail}</span>
}

/** The side-by-side preview, the swap, and the confirmation. */
function MergeReview({
  keep,
  fold,
  onSwap,
  onPickAnother,
  onMerged,
}: {
  keep: MergeTarget
  fold: MergeTarget
  onSwap: () => void
  onPickAnother: () => void
  onMerged: (survivor: ContactDetail, loserId: number) => void
}) {
  const queryClient = useQueryClient()
  const preview = useQuery(mergePreviewQuery(keep.id, fold.id))
  // Once the preview is in, both names carry the details it has in full.
  const keepName = preview.data ? labelledDetail(preview.data.survivor) : keep.name
  const foldName = preview.data ? labelledDetail(preview.data.loser) : fold.name
  const [confirming, setConfirming] = useState(false)
  const merge = useMutation({
    mutationFn: () => mergeContacts(keep.id, fold.id),
    onSuccess: (survivor) => {
      // Drop every preview first: one of these two, refetched now, would only answer 409.
      queryClient.removeQueries({ queryKey: [...contactsKeys.all, 'merge-preview'] })
      queryClient.setQueryData(contactsKeys.detail(survivor.id), survivor)
      void queryClient.invalidateQueries({ queryKey: contactsKeys.all })
      setConfirming(false)
      onMerged(survivor, fold.id)
    },
  })

  return (
    <div className="flex flex-col gap-3">
      <p>
        <span className="font-medium">{keepName}</span> stays.{' '}
        <span className="font-medium">{foldName}</span> is merged into them and its link leads there
        from now on.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" onClick={onSwap} disabled={merge.isPending}>
          <ArrowLeftRight data-icon="inline-start" />
          Keep {foldName} instead
        </Button>
        <Button size="sm" variant="ghost" onClick={onPickAnother} disabled={merge.isPending}>
          Choose another contact
        </Button>
      </div>

      {preview.isPending && <p role="status">Working out what the merge would do…</p>}
      {preview.isError && (
        <p role="alert" className="text-destructive">
          These two can’t be merged: {preview.error.message}
        </p>
      )}
      {preview.isSuccess && (
        <>
          <PreviewTable
            survivor={preview.data.survivor}
            loser={preview.data.loser}
            result={preview.data.result}
          />
          <MovesList moves={preview.data.moves} />
          <div>
            <Button size="sm" variant="destructive" onClick={() => setConfirming(true)}>
              Merge…
            </Button>
          </div>
          <ConfirmDialog
            open={confirming}
            onOpenChange={(open) => {
              if (merge.isPending) return
              setConfirming(open)
              if (!open) merge.reset()
            }}
            title={`Merge ${foldName} into ${keepName}?`}
            confirmLabel="Merge"
            pending={merge.isPending}
            error={merge.error?.message ?? null}
            onConfirm={() => merge.mutateAsync()}
          >
            <p>
              {keepName} keeps its record. Everything below moves to it from {foldName}, and{' '}
              {foldName} stops showing anywhere.
            </p>
            <ul className="list-disc pl-5">
              {describeMoves(preview.data.moves).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <p className="font-medium text-foreground">
              A merge can’t be undone. An import that created either contact can’t be rolled back
              afterward.
            </p>
          </ConfirmDialog>
        </>
      )}
    </div>
  )
}

/** The rows the preview compares: what a person checks before saying two are one. */
const ROWS: ReadonlyArray<{ label: string; value: (contact: ContactDetail) => ReactNode }> = [
  { label: 'Name', value: (c) => displayName(c) },
  { label: 'First and last', value: (c) => `${c.first_name} ${c.last_name}`.trim() || null },
  { label: 'Headline', value: (c) => c.headline },
  { label: 'Title', value: (c) => c.current_title },
  { label: 'Company', value: (c) => c.current_company },
  { label: 'Location', value: (c) => c.location },
  { label: 'LinkedIn id', value: (c) => c.li_public_id },
  { label: 'LinkedIn URN', value: (c) => c.li_urn },
  {
    label: 'Met',
    value: (c) => `${MET_LABELS[c.met]}${c.met_source === 'automatic' ? ' (by a batch)' : ''}`,
  },
  { label: 'Email', value: (c) => c.emails.map((row) => row.email).join(', ') || null },
  {
    label: 'Phone',
    value: (c) => c.phones.map((row) => row.number_e164 ?? row.raw).join(', ') || null,
  },
  { label: 'Do not contact', value: (c) => (c.do_not_contact ? 'Yes' : 'No') },
  { label: 'Needs review', value: (c) => (c.needs_review_at === null ? 'No' : 'Yes') },
  { label: 'Archived', value: (c) => formatDateTime(c.archived_at) ?? 'No' },
  { label: 'Notes', value: (c) => c.notes },
]

function PreviewTable({
  survivor,
  loser,
  result,
}: {
  survivor: ContactDetail
  loser: ContactDetail
  result: ContactDetail
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[32rem] border-collapse text-left text-sm">
        <caption className="pb-2 text-left text-muted-foreground">
          What survives: the after column is the merge itself, run and rolled back.
        </caption>
        <thead>
          <tr className="border-b">
            <th scope="col" className="py-1 pr-3 font-medium">
              Field
            </th>
            <th scope="col" className="py-1 pr-3 font-medium">
              Stays: {labelledDetail(survivor)}
            </th>
            <th scope="col" className="py-1 pr-3 font-medium">
              Merged away: {labelledDetail(loser)}
            </th>
            <th scope="col" className="py-1 font-medium">
              After the merge
            </th>
          </tr>
        </thead>
        <tbody>
          {ROWS.map((row) => (
            <tr key={row.label} className="border-b align-top last:border-0">
              <th scope="row" className="py-1 pr-3 font-normal text-muted-foreground">
                {row.label}
              </th>
              <td className="py-1 pr-3 break-words">{row.value(survivor) ?? '—'}</td>
              <td className="py-1 pr-3 break-words">{row.value(loser) ?? '—'}</td>
              <td className="py-1 break-words font-medium">{row.value(result) ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function MovesList({ moves }: { moves: MergeMoves }) {
  const lines = describeMoves(moves)
  return (
    <div>
      <p className="font-medium">What moves</p>
      <ul aria-label="What moves" className="list-disc pl-5 text-muted-foreground">
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </div>
  )
}
