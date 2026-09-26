/**
 * Guards against losing unsaved edits, the frontend's one pattern for it.
 *
 * A page that holds a draft renders `<UnsavedChangesGuard when={dirty} />`.
 * While `when` is true, leaving the page through the router (a link, back or
 * forward) stops on a confirm, and closing or reloading the tab gets the
 * browser's own "leave site?" prompt through `beforeunload`. A change of
 * selection inside the page, which the router never sees, asks through
 * `DiscardChangesDialog` too, so both read the same.
 */
import { useBlocker } from '@tanstack/react-router'

import { ConfirmDialog } from '@/components/ui/confirm-dialog'

// Stable, so the router registers the blocker once per `when` change, not once per render.
const block = () => true

export function UnsavedChangesGuard({ when }: { when: boolean }) {
  const blocker = useBlocker({
    shouldBlockFn: block,
    enableBeforeUnload: true,
    disabled: !when,
    withResolver: true,
  })
  return (
    <DiscardChangesDialog
      open={blocker.status === 'blocked'}
      onDiscard={() => blocker.proceed?.()}
      onKeep={() => blocker.reset?.()}
    />
  )
}

interface DiscardChangesDialogProps {
  open: boolean
  onDiscard: () => void
  onKeep: () => void
}

export function DiscardChangesDialog({ open, onDiscard, onKeep }: DiscardChangesDialogProps) {
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onKeep()
      }}
      title="Discard unsaved changes?"
      confirmLabel="Discard changes"
      onConfirm={onDiscard}
    >
      <p>Your edits haven't been saved. Cancel to go back to them.</p>
    </ConfirmDialog>
  )
}
