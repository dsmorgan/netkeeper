/**
 * The `/lists` page: lists, tags and their rules, and saved views.
 *
 * Spec 14.3 gives lists a page and gives tags none of their own — they belong
 * to a contact and to the rules that assign them. The three tabs here are the
 * three ways a group of contacts gets defined: by hand (a static list), by a
 * filter (a smart list), or by what they are called (a tag and its rules), with
 * saved views alongside as the table's own memory.
 */
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

import { CRM_TABS, type CrmTab } from './crm-tabs'
import { ListsPanel } from './lists-panel'
import { SavedViewsPanel } from './saved-views-panel'
import { TagsPanel } from './tags-panel'

export interface CrmPageProps {
  /** The open tab, which lives in the URL so a link can name one (#134). */
  tab: CrmTab
  onTabChange: (tab: CrmTab) => void
}

export function CrmPage({ tab, onTabChange }: CrmPageProps) {
  return (
    <Tabs
      value={tab}
      onValueChange={(value: unknown) => {
        const next = CRM_TABS.find((candidate) => candidate === value)
        if (next !== undefined) onTabChange(next)
      }}
      className="space-y-4"
    >
      <TabsList>
        <TabsTrigger value="lists">Lists</TabsTrigger>
        <TabsTrigger value="tags">Tags and rules</TabsTrigger>
        <TabsTrigger value="views">Saved views</TabsTrigger>
      </TabsList>
      <TabsContent value="lists">
        <ListsPanel />
      </TabsContent>
      <TabsContent value="tags">
        <TagsPanel />
      </TabsContent>
      <TabsContent value="views">
        <SavedViewsPanel />
      </TabsContent>
    </Tabs>
  )
}
