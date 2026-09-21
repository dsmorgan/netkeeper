import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, X } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Menu, MenuContent, MenuGroupLabel, MenuItem, MenuTrigger } from '@/components/ui/menu'

import { contactTagsQuery, contactsKeys, tagContact, tagsQuery, untagContact } from './api'
import { WriteError } from './merged-notice'

/**
 * The contact's tags (spec 10.3).
 *
 * Manual, automatic, and LLM tags all show; taking an automatic one off
 * suppresses it, which is the server's business, so the button says nothing
 * about which kind it is.
 */
export function ContactTags({ contactId }: { contactId: number }) {
  const queryClient = useQueryClient()
  const all = useQuery(tagsQuery)
  const carried = useQuery(contactTagsQuery(contactId))

  const write = useMutation({
    mutationFn: (run: () => Promise<void>) => run(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: contactsKeys.tags(contactId) })
      void queryClient.invalidateQueries({ queryKey: ['tags'] })
    },
  })

  if (carried.isPending) return <p className="text-muted-foreground">Loading tags…</p>
  if (carried.isError) {
    return (
      <p role="alert" className="text-destructive">
        Tags could not be loaded: {carried.error.message}
      </p>
    )
  }

  const byId = new Map((all.data ?? []).map((tag) => [tag.id, tag]))
  const on = new Set(carried.data.map((row) => row.tag_id))
  const available = (all.data ?? []).filter((tag) => !on.has(tag.id))

  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {carried.data.length === 0 && <span className="text-muted-foreground">No tags.</span>}
        {carried.data.map((row) => {
          const name = byId.get(row.tag_id)?.name ?? `tag ${row.tag_id}`
          return (
            <Badge key={row.id} variant="outline" className="gap-1 pr-1">
              {name}
              <Button
                size="icon-xs"
                variant="ghost"
                aria-label={`Remove tag ${name}`}
                disabled={write.isPending}
                onClick={() => write.mutate(() => untagContact(contactId, row.tag_id))}
              >
                <X />
              </Button>
            </Badge>
          )
        })}

        <Menu>
          <MenuTrigger
            render={
              <Button size="xs" variant="outline">
                <Plus data-icon="inline-start" />
                Add tag
              </Button>
            }
          />
          <MenuContent align="start" className="max-h-80">
            <MenuGroupLabel>Tags</MenuGroupLabel>
            {all.isPending && <MenuItem disabled>Loading tags…</MenuItem>}
            {all.isSuccess && available.length === 0 && (
              <MenuItem disabled>Every tag is already on</MenuItem>
            )}
            {available.map((tag) => (
              <MenuItem
                key={tag.id}
                onClick={() => write.mutate(() => tagContact(contactId, tag.id))}
              >
                {tag.name}
              </MenuItem>
            ))}
          </MenuContent>
        </Menu>
      </div>
      <WriteError error={write.error} />
    </div>
  )
}
