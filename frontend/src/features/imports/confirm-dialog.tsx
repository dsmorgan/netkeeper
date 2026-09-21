import { AlertDialog } from '@base-ui/react/alert-dialog'
import type { ReactNode } from 'react'

import { Button } from '@/components/ui/button'

import { ErrorNote } from './notes'

interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  /** What the action does, in full. A destructive action earns the whole sentence. */
  children: ReactNode
  confirmLabel: string
  onConfirm: () => void
  pending?: boolean
  /** A failure from the action itself, shown here because the dialog stays open. */
  error?: string | null
}

/**
 * A modal that states a destructive action's consequences and waits.
 *
 * It lives beside the import pages because nothing else needs it yet; move it
 * to `components/ui/` when a second feature does.
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
}: ConfirmDialogProps) {
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
            <div className="mt-3">
              <ErrorNote>{error}</ErrorNote>
            </div>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <AlertDialog.Close render={<Button variant="outline" />} disabled={pending}>
              Cancel
            </AlertDialog.Close>
            <Button variant="destructive" onClick={onConfirm} disabled={pending}>
              {pending ? 'Working…' : confirmLabel}
            </Button>
          </div>
        </AlertDialog.Popup>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  )
}
