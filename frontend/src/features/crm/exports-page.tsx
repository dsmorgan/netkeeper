/**
 * The `/exports` page: build the selection, pick the preset and format, download.
 *
 * The filter here is the same builder a smart list uses, so "the people who get
 * this file" is the same kind of visible, re-runnable definition as "the people
 * in this list". The reference table below lists every preset and format
 * together, because choosing between them is the one decision this page makes
 * and the differences are not guessable from their names.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { tagsQuery } from './api'
import { ExportForm } from './export-dialog'
import { EXPORT_PRESETS } from './export-presets'
import { FilterBuilder } from './filter-builder'
import { emptyTree } from './tree'
import type { FilterTree } from './types'

export function ExportsPage() {
  const tags = useQuery(tagsQuery)
  const [filter, setFilter] = useState<FilterTree>(emptyTree())

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_24rem]">
      <Card>
        <CardHeader>
          <CardTitle>Who to export</CardTitle>
          <CardDescription>
            No conditions means every live contact. The export runs this filter through the same
            compiler the contacts table and smart lists use.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <FilterBuilder value={filter} onChange={setFilter} tags={tags.data ?? []} />
        </CardContent>
      </Card>

      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>The file</CardTitle>
          </CardHeader>
          <CardContent>
            <ExportForm filter={filter} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>What each preset holds</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {EXPORT_PRESETS.map((preset) => (
              <div key={preset.value}>
                <h4 className="text-sm font-medium">{preset.label}</h4>
                <p className="text-xs text-muted-foreground">{preset.description}</p>
                {preset.caveat !== undefined && (
                  <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">{preset.caveat}</p>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
