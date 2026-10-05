import { useEffect, useState } from 'react'

/**
 * The time, again every `intervalMs`, so "3 min ago" keeps counting between
 * refetches. A refetch that brings the same data does not render on its own.
 */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return now
}
