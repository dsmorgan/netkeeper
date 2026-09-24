import { AlertDialog } from '@base-ui/react/alert-dialog'
import type { VariantProps } from 'class-variance-authority'
import { AlertTriangle } from 'lucide-react'
import type { ReactNode } from 'react'
import { useEffect, useRef } from 'react'

import { Button, type buttonVariants } from '@/components/ui/button'

interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  /** What the action does, in full. A consequential action earns the whole sentence. */
  children: ReactNode
  /** The button's label at rest. While `pending` is true it always reads "Working…"
   *  instead — a caller does not need (and should not build) a pending variant of it. */
  confirmLabel: string
  onConfirm: () => void
  pending?: boolean
  /** A failure from the action itself, shown here because the dialog stays open. */
  error?: string | null
  /**
   * The confirm button's style. Defaults to `destructive` (undo, delete, roll
   * back); a consequential-but-not-destructive action (arm scheduled runs,
   * start a run) can pass `default` so the button does not read as "this
   * deletes something".
   */
  confirmVariant?: VariantProps<typeof buttonVariants>['variant']
}

/**
 * A modal that states a consequential action's effects and waits for an
 * explicit confirmation. Base UI's `AlertDialog` traps focus inside it and
 * returns it to the trigger on close, so this needs nothing extra for that.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  children,
  confirmLabel,
  onConfirm,
  pending = false,
  error = null,
  confirmVariant = 'destructive',
}: ConfirmDialogProps) {
  // `disabled={pending}` alone is not enough: `pending` only flips after the
  // caller's mutation reports back, one render later, so two clicks inside the
  // same tick both fire before React ever disables the button. This ref is
  // checked and set synchronously in the handler itself, so the second of two
  // same-tick clicks is dropped regardless of render timing (L2).
  //
  // `pending` alone is not enough to *reset* it either: a mutation that
  // settles (succeeds or fails) fast enough that React never commits a render
  // with `pending` true — an instant 409, a fast test, a fast backend — means
  // `pending` reads `false` before the click and `false` after, so an effect
  // that only reacts to `pending` changing never runs again, and the ref
  // stays set forever: every click after the first is silently dropped, even
  // once the caller is plainly ready for another one. `open` (closes on
  // success) and `error` (appears on failure) are the two props that *do*
  // change whenever the action actually concludes, whatever `pending` did
  // along the way, so this resets on either of them too — reset the moment
  // `pending` turns true is harmless, since `disabled={pending}` on the
  // button below is what guards every click from then on; this ref only has
  // to survive the gap before `pending`'s first true render.
  const submitting = useRef(false)
  useEffect(() => {
    submitting.current = false
  }, [pending, open, error])

  const handleConfirm = (): void => {
    if (submitting.current) return
    submitting.current = true
    onConfirm()
  }

  return (
    <AlertDialog.Root open={open} onOpenChange={onOpenChange}>
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className="fixed inset-0 bg-black/40" />
        <AlertDialog.Popup className="fixed top-1/2 left-1/2 w-[min(32rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-xl bg-card p-5 text-sm text-card-foreground shadow-lg ring-1 ring-foreground/10">
          <AlertDialog.Title className="font-heading text-base font-medium">
            {title}
          </AlertDialog.Title>
          <AlertDialog.Description
            render={<div className="mt-2 flex flex-col gap-2 text-muted-foreground" />}
          >
            {children}
          </AlertDialog.Description>
          {error !== null && (
            <p
              role="alert"
              className="mt-3 flex items-start gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-destructive"
            >
              <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              <span>{error}</span>
            </p>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <AlertDialog.Close render={<Button variant="outline" />} disabled={pending}>
              Cancel
            </AlertDialog.Close>
            <Button variant={confirmVariant} onClick={handleConfirm} disabled={pending}>
              {pending ? 'Working…' : confirmLabel}
            </Button>
          </div>
        </AlertDialog.Popup>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  )
}
