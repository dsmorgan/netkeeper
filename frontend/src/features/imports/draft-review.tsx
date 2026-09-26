import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

import { ApiError, commitRun, fetchRows, importKeys, previewQuery } from './api'
import { CandidatesStep, type CandidateRow } from './candidates-step'
import { CommitResult, CommitStep } from './commit-step'
import { PreviewStep } from './preview-step'
import { StepNav } from './step-nav'
import type { ColumnMapping, Decision, ImportRun, PreviewRow } from './types'

/** Candidates fetched per page; the API caps a page at 500. */
const CANDIDATE_PAGE = 100

type ReviewStep = 'preview' | 'candidates' | 'commit'

interface DraftReviewProps {
  run: ImportRun
  mapping: ColumnMapping
  /** Null when the run came from history and the file is no longer in hand. */
  onBackToMapping: (() => void) | null
  onRestart: () => void
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * The half of the wizard that works on a draft run: preview, candidates, commit.
 *
 * It is separate from the upload and mapping screens so a draft left unfinished
 * can be picked up from its history page without the file being re-read.
 *
 * The counts stored on the run are what the file meant when it was read, and a
 * draft can be days old. `commit` re-resolves every row against the database as
 * it is now, so what it will refuse is what the *preview* says, not what the
 * draft recorded. Everything here that gates the commit is built from both: the
 * stored candidate rows, and the live resolutions the preview just came back
 * with. When the two disagree the draft has gone stale, and the screen says so
 * rather than offering a commit that answers 409.
 */
export function DraftReview({ run, mapping, onBackToMapping, onRestart }: DraftReviewProps) {
  const [step, setStep] = useState<ReviewStep>('preview')
  const [decisions, setDecisions] = useState<Record<number, Decision>>({})
  const [skipUndecided, setSkipUndecided] = useState(false)
  const [committed, setCommitted] = useState<ImportRun | null>(null)
  const queryClient = useQueryClient()

  const preview = useQuery(previewQuery(run.id))

  const liveCandidates = useMemo(
    () => (preview.data ?? []).filter((row) => row.resolution === 'candidate'),
    [preview.data],
  )

  const candidates = useInfiniteQuery({
    queryKey: ['imports', 'run', run.id, 'candidates'] as const,
    queryFn: ({ pageParam, signal }) =>
      fetchRows(
        run.id,
        { resolution: 'candidate', limit: CANDIDATE_PAGE, offset: pageParam },
        signal,
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((count, page) => count + page.items.length, 0)
      return loaded < lastPage.total ? loaded : undefined
    },
    // Also when the draft says there are none but the preview has just found
    // one: that is the stale draft, and it still needs the stored rows.
    enabled: (run.candidate_count > 0 || liveCandidates.length > 0) && committed === null,
    refetchOnWindowFocus: false,
  })

  const storedRows = useMemo(
    () => candidates.data?.pages.flatMap((page) => page.items) ?? [],
    [candidates.data],
  )
  const storedTotal = candidates.data?.pages[0]?.total ?? run.candidate_count

  /** Rows the preview now calls candidates that the draft never recorded as one. */
  const freshCandidates = useMemo(() => {
    const stored = new Set(storedRows.map((row) => row.row_number))
    return liveCandidates.filter((row) => !stored.has(row.row_number))
  }, [liveCandidates, storedRows])

  const stale = freshCandidates.length > 0

  /** Every row that needs an answer: the draft's, plus the ones since found. */
  const candidateRows: CandidateRow[] = useMemo(
    () => [...storedRows, ...freshCandidates].sort((a, b) => a.row_number - b.row_number),
    [storedRows, freshCandidates],
  )

  // `storedTotal` counts candidates past the page loaded; `candidateRows` counts
  // the ones found since. Neither alone is the whole answer, so take the larger.
  const candidateTotal = Math.max(storedTotal, candidateRows.length)
  const anyCandidates = candidateTotal > 0

  const previewsByRow = useMemo(() => {
    const byRow = new Map<number, PreviewRow>()
    for (const row of preview.data ?? []) byRow.set(row.row_number, row)
    return byRow
  }, [preview.data])

  const undecided = Math.max(candidateTotal - Object.keys(decisions).length, 0)

  const commit = useMutation({
    mutationFn: () => commitRun(run.id, Object.values(decisions), skipUndecided),
    onSuccess: (applied) => {
      setCommitted(applied)
      void queryClient.invalidateQueries({ queryKey: importKeys.all })
    },
  })

  // A 409 means the API re-resolved a row into a candidate this screen did not
  // know about. Whatever the counts say, skipping is then a real way out and
  // has to be on offer, or the import is stuck with no move left.
  const refusedAsUndecided =
    commit.isError && commit.error instanceof ApiError && commit.error.status === 409

  function decide(decision: Decision) {
    setDecisions((current) => ({ ...current, [decision.row_number]: decision }))
    // The refusal was about the decisions as they were; clear it so a decided
    // row is not still held behind the 409 that named it.
    if (commit.isError) commit.reset()
  }

  // This commit's own result comes first. The run query is invalidated by the
  // commit, so `run` arrives back as `committed` a moment later; reading its
  // status before this would throw away the screen the person was waiting for.
  if (committed !== null) {
    return <CommitResult run={committed} onRestart={onRestart} />
  }

  if (run.status !== 'draft') {
    return (
      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle level={2}>This import is already {run.status.replace('_', ' ')}</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-muted-foreground">
            Open it from the history to see what it did, or roll it back.
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <StepNav current={step} />

      {step === 'preview' && (
        <PreviewStep
          run={run}
          rows={preview.data}
          mapping={mapping}
          liveCandidates={liveCandidates.length}
          stale={stale}
          pending={preview.isPending}
          error={preview.isError ? message(preview.error) : null}
          onRetry={() => void preview.refetch()}
          onBack={onBackToMapping}
          onContinue={() => setStep(anyCandidates ? 'candidates' : 'commit')}
        />
      )}

      {step === 'candidates' && (
        <CandidatesStep
          run={run}
          rows={candidateRows}
          total={candidateTotal}
          stale={stale}
          previews={previewsByRow}
          mapping={mapping}
          decisions={decisions}
          onDecide={decide}
          onDecideRestAsNew={() =>
            setDecisions((current) => {
              const next = { ...current }
              for (const row of candidateRows) {
                next[row.row_number] ??= {
                  row_number: row.row_number,
                  kind: 'create_new',
                  contact_id: null,
                }
              }
              if (commit.isError) commit.reset()
              return next
            })
          }
          onLoadMore={() => void candidates.fetchNextPage()}
          canLoadMore={candidates.hasNextPage}
          pending={candidates.isPending || candidates.isFetchingNextPage}
          error={candidates.isError ? message(candidates.error) : null}
          onBack={() => setStep('preview')}
          onContinue={() => setStep('commit')}
        />
      )}

      {step === 'commit' && (
        <CommitStep
          run={run}
          decisions={decisions}
          candidateTotal={candidateTotal}
          undecided={undecided}
          refused={refusedAsUndecided}
          skipUndecided={skipUndecided}
          onSkipChange={setSkipUndecided}
          onCommit={() => commit.mutate()}
          pending={commit.isPending}
          error={commit.isError ? message(commit.error) : null}
          onBack={() => setStep(anyCandidates ? 'candidates' : 'preview')}
        />
      )}
    </div>
  )
}
