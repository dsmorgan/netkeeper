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
  /**
   * The action. Return its promise (`mutation.mutateAsync(...)`, not `mutate`):
   * the dialog takes another confirm only once that promise settles, whatever
   * `pending` did meanwhile (#181). A rejection is the caller's to show through
   * `error`; the dialog swallows it here so it never surfaces as unhandled.
   * A `void` return is accepted for a caller not yet moved over, and falls back
   * to resetting on `pending`, `open`, or `error` changing, which can jam on two
   * fast failures with the same text.
   */
  onConfirm: () => Promise<unknown> | void
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
  /** Disables the confirm button (not Cancel) while the action cannot be taken, such as
   *  while what it acts on is loading or was refused. */
  confirmDisabled?: boolean
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
  confirmDisabled = false,
}: ConfirmDialogProps) {
  // `disabled={pending}` alone is not enough: `pending` only flips after the
  // caller's mutation reports back, one render later, so two clicks inside the
  // same tick both fire before React ever disables the button. This ref is
  // checked and set synchronously in the handler itself, so the second of two
  // same-tick clicks is dropped regardless of render timing (L2).
  //
  // It resets when `onConfirm`'s promise settles (#181). Nothing short of that
  // is reliable: a mutation that fails fast enough that React never commits a
  // render with `pending` true, twice with the same error text, changes none
  // of `pending`, `open`, or `error`, so a reset that watched only those props
  // never ran and every later click was dropped. The effect below stays only
  // for a caller that still returns `void`; for one that returns its promise,
  // a reset it makes early is harmless, since `disabled={pending}` guards
  // every click once `pending` renders true.
  const submitting = useRef(false)
  useEffect(() => {
    submitting.current = false
  }, [pending, open, error])

  const handleConfirm = (): void => {
    if (submitting.current) return
    submitting.current = true
    let result: Promise<unknown> | void
    try {
      result = onConfirm()
    } catch (thrown) {
      submitting.current = false
      throw thrown
    }
    if (result instanceof Promise) {
      void result
        .catch(() => {
          // The caller shows the failure through `error`.
        })
        .finally(() => {
          submitting.current = false
        })
    }
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
            <Button
              variant={confirmVariant}
              onClick={handleConfirm}
              disabled={pending || confirmDisabled}
            >
              {pending ? 'Working…' : confirmLabel}
            </Button>
          </div>
        </AlertDialog.Popup>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  )
}
