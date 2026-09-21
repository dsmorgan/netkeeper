import { useEffect, useState } from 'react'

/**
 * `value` after it has held still for `delay` milliseconds.
 *
 * The filter builder and the rule editor both count against the whole address
 * book, so they ask on the value that settled rather than on every keystroke.
 */
export function useDebounced<T>(value: T, delay = 300): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return settled
}
