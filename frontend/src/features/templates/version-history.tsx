/**
 * The versions of one template, newest first. An older version opens read-only.
 */
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { formatDateTime } from '@/features/contacts/format'

import type { TemplateOut } from './api'

interface VersionHistoryProps {
  versions: readonly TemplateOut[]
  viewingId: number
  onView: (id: number) => void
}

export function VersionHistory({ versions, viewingId, onView }: VersionHistoryProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle level={3}>Version history</CardTitle>
        <CardDescription>
          Saving a template that a campaign uses creates a new version. The campaign keeps the
          version it started with, unchanged. Older versions are read-only.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ol aria-label="Versions" className="divide-y rounded-lg border">
          {versions.map((row) => (
            <li key={row.id} className="flex items-center gap-2 px-3 py-2 text-sm">
              <span className="font-medium">Version {row.version}</span>
              {row.current ? (
                <Badge variant="secondary">Current</Badge>
              ) : (
                <Badge variant="outline">Read-only</Badge>
              )}
              <span className="text-xs text-muted-foreground">
                saved {formatDateTime(row.updated_at)}
              </span>
              <Button
                size="sm"
                variant={row.id === viewingId ? 'secondary' : 'ghost'}
                className="ml-auto"
                aria-pressed={row.id === viewingId}
                aria-label={`View version ${row.version}`}
                onClick={() => onView(row.id)}
              >
                {row.id === viewingId ? 'Viewing' : 'View'}
              </Button>
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  )
}
