/**
 * Templates (spec 11.1, 14.3; item P3-10): the list, the editor with lint as
 * you type, a preview against a contact you pick, and the version history.
 *
 * An unsaved draft is never thrown away without asking: opening another
 * template, starting a new one, or leaving the page confirms first, and closing
 * the tab gets the browser's own prompt.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { useEffect, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { DiscardChangesDialog, UnsavedChangesGuard } from '@/components/unsaved-changes'
import { Callout, EmptyState, ErrorNote, LoadingNote } from '@/features/crm/controls'

import { templateKeys, templatesQuery, versionsQuery } from './api'
import type { TemplateOut } from './api'
import { LintList } from './lint-list'
import { PreviewPanel } from './preview-panel'
import { draftOf, sameDraft } from './draft'
import { TemplateEditor } from './template-editor'
import { VersionHistory } from './version-history'

type Selection = number | 'new' | null

/** Said once after a save that made a new version, so it cannot pass unnoticed. */
interface NewVersionNotice {
  name: string
  from: number
  to: number
}

export function TemplatesPage() {
  const client = useQueryClient()
  const templates = useQuery(templatesQuery)
  const [selected, setSelected] = useState<Selection>(null)
  const [notice, setNotice] = useState<NewVersionNotice | null>(null)
  // Whether the open workspace holds edits that aren't saved; it reports this itself.
  const [dirty, setDirty] = useState(false)
  // Where a click would go, held while the discard confirm is open.
  const [asked, setAsked] = useState<{ next: Selection } | null>(null)

  const go = (next: Selection) => {
    setNotice(null)
    setSelected(next)
  }
  const select = (next: Selection) => {
    if (dirty && next !== selected) setAsked({ next })
    else go(next)
  }
  const onSaved = (before: TemplateOut | null, saved: TemplateOut) => {
    void client.invalidateQueries({ queryKey: templateKeys.all })
    setSelected(saved.id)
    setNotice(
      before !== null && saved.id !== before.id
        ? { name: saved.name, from: before.version, to: saved.version }
        : null,
    )
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Templates</h1>
      <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
        <Card>
          <CardHeader>
            <CardTitle level={2}>All templates</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Button variant="outline" className="w-full" onClick={() => select('new')}>
              <Plus aria-hidden /> New template
            </Button>
            {templates.isPending && <LoadingNote label="Loading templates…" />}
            {templates.isError && (
              <ErrorNote label="Could not load the templates" error={templates.error} />
            )}
            {templates.data !== undefined &&
              (templates.data.length === 0 ? (
                <EmptyState title="No templates yet">
                  <p>Create one to use it in a campaign.</p>
                </EmptyState>
              ) : (
                <ul aria-label="Templates" className="space-y-1">
                  {templates.data.map((row) => (
                    <li key={row.id}>
                      <Button
                        variant={row.id === selected ? 'secondary' : 'ghost'}
                        className="w-full justify-start gap-2"
                        aria-current={row.id === selected ? 'true' : undefined}
                        onClick={() => select(row.id)}
                      >
                        <span className="truncate">{row.name}</span>
                        <span className="ml-auto flex gap-1">
                          {row.in_use && <Badge variant="outline">In use</Badge>}
                          {row.lint.length > 0 && (
                            <Badge variant="destructive">
                              {row.lint.length} lint {row.lint.length === 1 ? 'error' : 'errors'}
                            </Badge>
                          )}
                        </span>
                      </Button>
                    </li>
                  ))}
                </ul>
              ))}
          </CardContent>
        </Card>

        <div className="min-w-0 space-y-4">
          {notice !== null && (
            <Callout tone="info" title={`Saved as version ${notice.to}`}>
              <p>
                A campaign uses “{notice.name}”, so your edit was saved as version {notice.to}.
                Version {notice.from} stays as it was for that campaign, and is read-only.
              </p>
            </Callout>
          )}
          {selected === null ? (
            <EmptyState title="No template open">
              <p>Pick one from the list, or start a new one.</p>
            </EmptyState>
          ) : selected === 'new' ? (
            <Workspace
              key="new"
              current={null}
              onSaved={(saved) => onSaved(null, saved)}
              onDeleted={() => go(null)}
              onDirtyChange={setDirty}
            />
          ) : (
            <LoadedWorkspace
              key={selected}
              id={selected}
              onSaved={onSaved}
              onDeleted={() => {
                void client.invalidateQueries({ queryKey: templateKeys.all })
                go(null)
              }}
              onDirtyChange={setDirty}
            />
          )}
        </div>
      </div>
      <UnsavedChangesGuard when={dirty} />
      <DiscardChangesDialog
        open={asked !== null}
        onDiscard={() => {
          if (asked !== null) go(asked.next)
          setAsked(null)
        }}
        onKeep={() => setAsked(null)}
      />
    </div>
  )
}

interface LoadedWorkspaceProps {
  id: number
  onSaved: (before: TemplateOut, saved: TemplateOut) => void
  onDeleted: () => void
  onDirtyChange: (dirty: boolean) => void
}

function LoadedWorkspace({ id, onSaved, onDeleted, onDirtyChange }: LoadedWorkspaceProps) {
  const versions = useQuery(versionsQuery(id))
  const current = versions.data?.[0]
  if (current === undefined) {
    if (versions.isError) {
      return <ErrorNote label="Could not load the template" error={versions.error} />
    }
    return <LoadingNote label="Loading the template…" />
  }
  return (
    <Workspace
      current={current}
      versions={versions.data}
      onSaved={(saved) => onSaved(current, saved)}
      onDeleted={onDeleted}
      onDirtyChange={onDirtyChange}
    />
  )
}

interface WorkspaceProps {
  current: TemplateOut | null
  versions?: readonly TemplateOut[]
  onSaved: (saved: TemplateOut) => void
  onDeleted: () => void
  /** Told whenever the draft starts or stops differing from what is saved, and false on unmount. */
  onDirtyChange: (dirty: boolean) => void
}

function Workspace({ current, versions, onSaved, onDeleted, onDirtyChange }: WorkspaceProps) {
  const [draft, setDraft] = useState(() => draftOf(current))
  const dirty = !sameDraft(draft, draftOf(current))
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange])
  useEffect(() => () => onDirtyChange(false), [onDirtyChange])
  const [viewingId, setViewingId] = useState<number | null>(current?.id ?? null)
  const older =
    current !== null && viewingId !== current.id
      ? versions?.find((row) => row.id === viewingId)
      : undefined

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <div className="min-w-0 space-y-4">
        {older !== undefined && current !== null ? (
          <OlderVersion row={older} onBack={() => setViewingId(current.id)} />
        ) : (
          <TemplateEditor
            template={current}
            draft={draft}
            onDraftChange={setDraft}
            onSaved={onSaved}
            onDeleted={onDeleted}
          />
        )}
      </div>
      <div className="min-w-0 space-y-4">
        <PreviewPanel
          templateId={viewingId}
          unsaved={older === undefined && current !== null && dirty}
        />
        {versions !== undefined && viewingId !== null && (
          <VersionHistory versions={versions} viewingId={viewingId} onView={setViewingId} />
        )}
      </div>
    </div>
  )
}

/** An older version, read-only: what a campaign that still uses it will send. */
function OlderVersion({ row, onBack }: { row: TemplateOut; onBack: () => void }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle level={3}>
          Version {row.version} <Badge variant="outline">Read-only</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <Callout>
          <p>
            An older version can't be edited. A campaign that started with it keeps sending it as it
            is here.
          </p>
        </Callout>
        <dl className="space-y-2">
          <div>
            <dt className="text-xs text-muted-foreground">Name</dt>
            <dd>{row.name}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Channel</dt>
            <dd>{row.channel === 'email' ? 'Email' : 'LinkedIn message'}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Subject</dt>
            <dd data-testid="version-subject">{row.subject ?? '(none)'}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Body</dt>
            <dd>
              <pre
                data-testid="version-body"
                className="rounded-lg border bg-muted/40 p-2 font-mono text-sm break-words whitespace-pre-wrap"
              >
                {row.body}
              </pre>
            </dd>
          </div>
        </dl>
        {row.lint.length > 0 && <LintList issues={row.lint} label="Lint at save" />}
        <Button variant="outline" onClick={onBack}>
          Back to the current version
        </Button>
      </CardContent>
    </Card>
  )
}
