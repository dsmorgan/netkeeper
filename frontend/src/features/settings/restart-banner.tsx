import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'

import {
  type ConfigSettings,
  cancelRestart,
  configSettingsQuery,
  currentBootId,
  restartServe,
} from './api'

/**
 * **Restart netkeeper** (#597): shown while a setting netkeeper serve reads at startup
 * has changed, or config.toml changed on disk. The backend never stops serve in the
 * middle of work: while a LinkedIn run, the browser, a task or a campaign send is busy
 * it refuses, and **Restart when idle** asks it to wait for that work instead. Under the
 * login item launchd starts serve again, otherwise it re-execs itself. This waits for
 * `/health` to answer with a new boot id, then refetches everything.
 */
type Phase =
  | { kind: 'idle' }
  | { kind: 'waiting'; from: string; since: number }
  | { kind: 'back' }
  | { kind: 'lost' }

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function RestartBanner({
  settings,
  pollMs = 1_000,
  giveUpMs = 120_000,
}: {
  settings: ConfigSettings
  pollMs?: number
  giveUpMs?: number
}) {
  const queryClient = useQueryClient()
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' })
  const restart = useMutation({
    mutationFn: restartServe,
    onSuccess: (data) => setPhase({ kind: 'waiting', from: data.boot_id, since: Date.now() }),
  })
  const cancel = useMutation({
    mutationFn: cancelRestart,
    onSuccess: () => {
      setPhase({ kind: 'idle' })
      void queryClient.invalidateQueries({ queryKey: configSettingsQuery.queryKey })
    },
  })

  useEffect(() => {
    if (phase.kind !== 'waiting') return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let since = phase.since
    const check = async () => {
      const boot = await currentBootId()
      if (stopped) return
      if (boot !== null && boot !== phase.from) {
        setPhase({ kind: 'back' })
        void queryClient.invalidateQueries()
        return
      }
      if (boot === phase.from) {
        // Still the old server, waiting for its work to finish: keep what it waits on
        // current, and count the give-up time only once it stops answering.
        since = Date.now()
        void queryClient.invalidateQueries({ queryKey: configSettingsQuery.queryKey })
      } else if (Date.now() - since > giveUpMs) {
        setPhase({ kind: 'lost' })
        return
      }
      timer = setTimeout(() => void check(), pollMs)
    }
    timer = setTimeout(() => void check(), pollMs)
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [phase, pollMs, giveUpMs, queryClient])

  if (phase.kind === 'back') {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        netkeeper restarted, and the settings above are in force.
      </p>
    )
  }
  if (!settings.restart_pending && !settings.restart_waiting && phase.kind === 'idle') return null

  const asked = phase.kind === 'waiting'
  const waiting = settings.restart_waiting || asked
  const busy = settings.busy
  return (
    <section
      aria-label="Restart netkeeper"
      className="space-y-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-700 dark:bg-amber-950"
    >
      <p>
        {settings.config_changed
          ? 'config.toml changed since netkeeper started, '
          : 'A setting you changed '}
        takes effect when netkeeper restarts.
      </p>
      {settings.config_error !== null && (
        <p role="alert" className="text-destructive">
          config.toml does not load, so netkeeper would not start again: {settings.config_error}
        </p>
      )}
      {busy.length > 0 && (
        <div>
          <p>{waiting ? 'netkeeper restarts once this is done:' : 'netkeeper is busy:'}</p>
          <ul className="list-disc pl-5">
            {busy.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
      {settings.can_restart ? (
        <div className="flex flex-wrap items-center gap-2">
          {!waiting && (
            <Button
              type="button"
              disabled={restart.isPending || settings.config_error !== null}
              onClick={() => restart.mutate(busy.length > 0)}
            >
              {busy.length > 0 ? 'Restart when idle' : 'Restart netkeeper'}
            </Button>
          )}
          {waiting && (
            <span role="status" className="text-muted-foreground">
              {busy.length > 0
                ? 'Waiting to restart… nothing running is cut off.'
                : 'Restarting… this page reconnects when netkeeper is back.'}
            </span>
          )}
          {waiting && busy.length > 0 && (
            <Button
              type="button"
              variant="outline"
              disabled={cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              Cancel
            </Button>
          )}
        </div>
      ) : (
        <p className="text-muted-foreground">
          This netkeeper cannot restart itself (it runs with --reload, or not as netkeeper serve).
          Stop it and start it again.
        </p>
      )}
      {waiting && busy.length > 0 && (
        <p className="text-muted-foreground">
          Once nothing is running, netkeeper pauses scheduled LinkedIn runs and campaign sends
          before it restarts. A run you start by hand after that keeps them paused until it ends.
        </p>
      )}
      {cancel.isError && <p role="alert">{message(cancel.error)}</p>}
      {phase.kind === 'lost' && (
        <p role="alert" className="text-destructive">
          netkeeper has not come back. Check its log, or start it again with netkeeper serve.
        </p>
      )}
      {restart.isError && <p role="alert">{message(restart.error)}</p>}
    </section>
  )
}
