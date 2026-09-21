/**
 * The `?` overlay: the whole keyboard map, on screen, without the mouse.
 *
 * It is a dialog that is explicitly **not** modal. `aria-modal` is false, no
 * focus trap is installed, and the decision keys keep working while it is open:
 * a person can press `?`, read the map, and press `m` without closing it first.
 * A modal here would be the one thing that breaks a keyboard-first screen.
 *
 * Focus moves to the panel so a screen reader reads it, and returns to whatever
 * had it when the panel closes. Escape and `?` both close it.
 */

import { useEffect, useRef } from 'react'

import { Button } from '@/components/ui/button'

import { KEY_BINDINGS } from './keymap'

export function KeyboardHelp({ onClose }: { onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null)
  const returnTo = useRef<Element | null>(null)

  useEffect(() => {
    returnTo.current = document.activeElement
    panel.current?.focus()
    const target = returnTo.current
    return () => {
      if (target instanceof HTMLElement && document.contains(target)) target.focus()
    }
  }, [])

  return (
    <div
      ref={panel}
      role="dialog"
      aria-modal="false"
      aria-label="Keyboard shortcuts"
      tabIndex={-1}
      className="fixed inset-x-4 bottom-4 z-50 mx-auto max-w-lg rounded-xl bg-popover p-4 text-popover-foreground shadow-lg ring-1 ring-foreground/15 focus-visible:outline-none sm:inset-x-auto sm:right-4"
    >
      <div className="flex items-start justify-between gap-4">
        <h2 className="font-heading font-medium">Keyboard</h2>
        <Button size="xs" variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
        {KEY_BINDINGS.map((binding) => (
          <div key={binding.key} className="contents">
            <dt>
              <kbd className="rounded border px-1.5 py-0.5 font-mono text-xs">{binding.label}</kbd>
            </dt>
            <dd className="text-muted-foreground">{binding.description}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-3 text-xs text-muted-foreground">
        These keys keep working while this panel is open. Tab reaches the filters and the suggestion
        banner.
      </p>
    </div>
  )
}
