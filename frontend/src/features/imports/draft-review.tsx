import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { commitRun, fetchRows, importKeys, previewQuery } from './api'
import { CandidatesStep } from './candidates-step'
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
 */
export function DraftReview({ run, mapping, onBackToMapping, onRestart }: DraftReviewProps) {
  const [step, setStep] = useState<ReviewStep>('preview')
  const [decisions, setDecisions] = useState<Record<number, Decision>>({})
  const [skipUndecided, setSkipUndecided] = useState(false)
  const [committed, setCommitted] = useState<ImportRun | null>(null)
  const queryClient = useQueryClient()

  const preview = useQuery(previewQuery(run.id))

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
    enabled: run.candidate_count > 0 && committed === null,
    refetchOnWindowFocus: false,
  })

  const candidateRows = useMemo(
    () => candidates.data?.pages.flatMap((page) => page.items) ?? [],
    [candidates.data],
  )
  const candidateTotal = candidates.data?.pages[0]?.total ?? run.candidate_count

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

  if (committed !== null) {
    return <CommitResult run={committed} onRestart={onRestart} />
  }

  return (
    <div className="flex flex-col gap-4">
      <StepNav current={step} />

      {step === 'preview' && (
        <PreviewStep
          run={run}
          rows={preview.data}
          mapping={mapping}
          pending={preview.isPending}
          error={preview.isError ? message(preview.error) : null}
          onRetry={() => void preview.refetch()}
          onBack={onBackToMapping}
          onContinue={() => setStep(run.candidate_count > 0 ? 'candidates' : 'commit')}
        />
      )}

      {step === 'candidates' && (
        <CandidatesStep
          run={run}
          rows={candidateRows}
          total={candidateTotal}
          previews={previewsByRow}
          mapping={mapping}
          decisions={decisions}
          onDecide={(decision) =>
            setDecisions((current) => ({ ...current, [decision.row_number]: decision }))
          }
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
          undecided={undecided}
          skipUndecided={skipUndecided}
          onSkipChange={setSkipUndecided}
          onCommit={() => commit.mutate()}
          pending={commit.isPending}
          error={commit.isError ? message(commit.error) : null}
          onBack={() => setStep(run.candidate_count > 0 ? 'candidates' : 'preview')}
        />
      )}
    </div>
  )
}
