import { useMutation, useQueryClient } from '@tanstack/react-query'

import { contactsKeys } from './api'
import type { ContactDetail } from './types'

/**
 * Runs a write that answers with the whole contact, and keeps the detail cache
 * on the answer the server gave rather than on a guess.
 *
 * The table's pages are invalidated too: a name, a met value, or an archive
 * changes what a row shows.
 */
export function useContactWrite(contactId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (run: () => Promise<ContactDetail>) => run(),
    onSuccess: (detail) => {
      queryClient.setQueryData(contactsKeys.detail(contactId), detail)
      void queryClient.invalidateQueries({ queryKey: contactsKeys.pages() })
    },
  })
}
