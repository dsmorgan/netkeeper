import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { statusQuery } from './api'
import { BrowserCard } from './browser-card'
import { BudgetPanel, HeatPanel } from './budget-heat-panels'
import { InboxCheckCard } from './inbox-check-card'
import { PinsPanel } from './pins-panel'
import { RunDetail } from './run-detail'
import { RunsPanel } from './runs-panel'
import { ScheduleCard } from './schedule-card'
import { SessionBanner } from './session-banner'
import { StartRunCard } from './start-run-card'
import { useRunEvents } from './use-run-events'

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'The LinkedIn page is unavailable.'
}

/**
 * The LinkedIn page (issue #108, spec 14.3): browser status and launch
 * instructions, preflight results, runs with live progress and stop, budget
 * and heat, pins, and the session banner. Everything on it updates over the
 * app's one shared SSE connection (`useRunEvents`) — no reload, no poll.
 */
export function LinkedInPage() {
  const status = useQuery(statusQuery)
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)
  const detail = useRef<HTMLDivElement>(null)
  const scrollToDetail = useRef(false)
  useRunEvents()

  // A run opened from the runs list shows below it: bring it into view (#405), and
  // only then, not when a start or a resume selects one. Smooth unless the reader
  // asked for reduced motion; jsdom has neither call, hence the optional ones.
  useEffect(() => {
    if (!scrollToDetail.current || selectedRunId === null) return
    scrollToDetail.current = false
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    detail.current?.scrollIntoView?.({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' })
  }, [selectedRunId])

  return (
    <div className="flex max-w-6xl flex-col gap-4">
      <h1 className="sr-only">LinkedIn</h1>

      {status.isPending && (
        <p role="status" className="text-muted-foreground">
          Loading…
        </p>
      )}
      {status.isError && <p role="alert">{message(status.error)}</p>}
      {status.isSuccess && <SessionBanner status={status.data} />}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <ScheduleCard />
        <BrowserCard />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <BudgetPanel />
        <HeatPanel />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="flex flex-col gap-4">
          <RunsPanel
            selectedRunId={selectedRunId}
            onSelect={(runId) => {
              scrollToDetail.current = true
              setSelectedRunId(runId)
            }}
          />
          {selectedRunId !== null && (
            <div ref={detail}>
              <RunDetail runId={selectedRunId} onResumed={setSelectedRunId} />
            </div>
          )}
        </div>
        <div className="flex flex-col gap-4">
          <StartRunCard onStarted={setSelectedRunId} />
          <InboxCheckCard onStarted={setSelectedRunId} />
          <PinsPanel />
        </div>
      </div>
    </div>
  )
}
