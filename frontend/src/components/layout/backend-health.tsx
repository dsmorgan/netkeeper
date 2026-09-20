import { useQuery } from '@tanstack/react-query'

import { healthQuery } from '@/api/queries'
import { cn } from '@/lib/utils'

/** Top-bar liveness dot. Shares the `health` query with the dashboard. */
export function BackendHealth() {
  const health = useQuery(healthQuery)

  const tone = health.isPending ? 'pending' : health.isError ? 'down' : 'up'
  const label = health.isPending
    ? 'Checking backend'
    : health.isError
      ? 'Backend unreachable'
      : `Backend ok · ${health.data.version}`

  return (
    <div role="status" aria-live="polite" className="flex items-center gap-2 text-muted-foreground">
      <span
        aria-hidden="true"
        className={cn(
          'size-2 rounded-full',
          tone === 'up' && 'bg-emerald-500',
          tone === 'down' && 'bg-destructive',
          tone === 'pending' && 'bg-muted-foreground/50',
        )}
      />
      <span>{label}</span>
    </div>
  )
}
