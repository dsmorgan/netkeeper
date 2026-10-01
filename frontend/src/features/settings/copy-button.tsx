import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'

type CopyState = 'idle' | 'copied' | 'failed'

/** A value to paste somewhere else, shown in full, with a button that copies it. */
export function CopyValue({ label, value }: { label: string; value: string }) {
  const [state, setState] = useState<CopyState>('idle')

  useEffect(() => {
    if (state === 'idle') return
    const timer = setTimeout(() => setState('idle'), 2000)
    return () => clearTimeout(timer)
  }, [state])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setState('copied')
    } catch {
      setState('failed')
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-muted-foreground">{label}</span>
      <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs break-all">{value}</code>
      <Button
        type="button"
        variant="outline"
        size="xs"
        aria-label={`Copy ${label.toLowerCase()}`}
        onClick={() => void copy()}
      >
        {state === 'copied' ? 'Copied' : state === 'failed' ? 'Select it by hand' : 'Copy'}
      </Button>
    </div>
  )
}
