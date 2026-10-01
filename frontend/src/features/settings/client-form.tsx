import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { mailboxKeys, saveClient } from '@/features/mailboxes/api'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** The Desktop OAuth client's ID and secret, saved to the Keychain. */
export function ClientForm({ onCancel, onSaved }: { onCancel?: () => void; onSaved?: () => void }) {
  const queryClient = useQueryClient()
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')

  const save = useMutation({
    mutationFn: saveClient,
    onSuccess: async () => {
      setClientId('')
      setClientSecret('')
      onSaved?.()
      await queryClient.invalidateQueries({ queryKey: mailboxKeys.all })
    },
  })

  return (
    <form
      className="grid gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        save.mutate({ client_id: clientId, client_secret: clientSecret })
      }}
    >
      <Label htmlFor="gmail-client-id">Client ID</Label>
      <Input
        id="gmail-client-id"
        value={clientId}
        onChange={(event) => setClientId(event.target.value)}
        placeholder="….apps.googleusercontent.com"
        autoComplete="off"
        required
      />
      <Label htmlFor="gmail-client-secret">Client secret</Label>
      <Input
        id="gmail-client-secret"
        type="password"
        value={clientSecret}
        onChange={(event) => setClientSecret(event.target.value)}
        autoComplete="off"
        required
      />
      <p className="text-xs text-muted-foreground">
        Stored in the Keychain, never in the database.
      </p>
      {save.isError && (
        <p role="alert" className="text-destructive">
          {message(save.error)}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={save.isPending}>
          {save.isPending ? 'Saving…' : 'Save client'}
        </Button>
        {onCancel !== undefined && (
          <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  )
}
