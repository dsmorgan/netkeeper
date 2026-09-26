/**
 * The export dialog: four presets, three formats, and the caveats spelled out.
 *
 * Two of the caveats are the difference between a useful file and a surprise.
 *
 * `nine-column` round-trips the *file*, not the contact. Appendix A's
 * "First Name" column carries `preferred_name` on the way out, and the importer
 * reads that column back into `first_name`, so someone stored as
 * first_name "Robert" / preferred_name "Bob" exports as "Bob" and comes back as
 * "Bob" in both. That is the right call for a mail merge — the file has one
 * name column and a merge wants the name you address someone by — but it is not
 * something to discover afterwards, so the dialog says it where the preset is
 * chosen.
 *
 * `campaign-audience` leaves out everyone marked do-not-contact, by design:
 * producing a mail-merge file is a send path by proxy once the file leaves the
 * tool. Its row count is therefore lower than the count beside the filter, and
 * the dialog explains the gap instead of letting it read as a miscount.
 *
 * "Safe to open in a spreadsheet" (CSV only, off by default, #76) quotes any cell
 * that starts like a formula. Names and companies come from other people's
 * profiles, so `=HYPERLINK(…)` is attacker-chosen text a spreadsheet would run.
 * It stays opt-in because it changes the bytes: the file no longer re-imports,
 * and every `+1…` phone number gains a leading quote.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'

import { countFilter, exportUrl } from './api'
import { Callout } from './controls'
import { EXPORT_PRESETS } from './export-presets'
import { emptyTree, validateTree } from './tree'
import type { ExportFormat, ExportPreset, FilterTree } from './types'

const FORMATS: ReadonlyArray<{ value: ExportFormat; label: string; note: string }> = [
  { value: 'csv', label: 'CSV', note: 'One row per contact, for a spreadsheet or a mail merge.' },
  { value: 'json', label: 'JSON', note: 'An array of objects, with the child rows nested.' },
  { value: 'vcard', label: 'vCard', note: 'One card per contact, for an address book.' },
]

export interface ExportDialogProps {
  /** The filter to export; null exports every live contact. */
  filter: FilterTree | null
  /** Named in the dialog so it is obvious what is being exported. */
  listName?: string
  /** The list's own member count, to compare against the preset's row count. */
  listCount?: number
}

export function ExportDialog({ filter, listName, listCount }: ExportDialogProps) {
  const [open, setOpen] = useState(false)
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button variant="outline">
            <Download data-icon="inline-start" aria-hidden />
            Export
          </Button>
        }
      />
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Export {listName === undefined ? 'contacts' : `“${listName}”`}</DialogTitle>
          <DialogDescription>
            The export runs the filter you see, streams straight from the database, and never
            materializes the file.
          </DialogDescription>
        </DialogHeader>
        <ExportForm filter={filter} listCount={listCount} />
        <DialogFooter>
          <DialogClose render={<Button variant="ghost">Cancel</Button>} />
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** The dialog's body, exported on its own so the Exports page can render it without a dialog. */
export function ExportForm({ filter, listCount }: Omit<ExportDialogProps, 'listName'>) {
  const [preset, setPreset] = useState<ExportPreset>('nine-column')
  const [format, setFormat] = useState<ExportFormat>('csv')
  const [headerless, setHeaderless] = useState(false)
  const [spreadsheetSafe, setSpreadsheetSafe] = useState(false)

  const tree = filter ?? emptyTree()
  // An unfinished filter would come back as a 422 on a download the browser
  // has already navigated to, which is a blank tab and no explanation. Say it
  // here instead, and leave the link off until it would work.
  const issues = validateTree(tree)
  const count = useQuery({
    queryKey: ['contacts', 'count', JSON.stringify(tree)],
    queryFn: ({ signal }) => countFilter(tree, signal),
    enabled: issues.length === 0,
    retry: false,
    gcTime: 0,
  })

  const chosen = EXPORT_PRESETS.find((candidate) => candidate.value === preset)
  const chosenFormat = FORMATS.find((candidate) => candidate.value === format)
  const isCsv = format === 'csv'
  const href = exportUrl({
    preset,
    format,
    headerless: headerless && isCsv,
    spreadsheetSafe: spreadsheetSafe && isCsv,
    filter,
  })

  return (
    <div className="space-y-3">
      <p role="status" className="text-sm">
        {issues.length > 0 && 'Finish the filter to see what would be exported.'}
        {issues.length === 0 && count.isPending && 'Counting the selection…'}
        {issues.length === 0 &&
          count.isError &&
          'Could not count the selection; the export will still run.'}
        {count.data !== undefined && issues.length === 0 && (
          <>
            <strong>{count.data.total.toLocaleString()}</strong> contacts selected —{' '}
            <span className="text-muted-foreground">{count.data.describe}</span>
            {chosen?.dropsRows === true && (
              <> The file may hold fewer: this preset leaves some out, as explained below.</>
            )}
          </>
        )}
      </p>

      <div className="grid gap-1">
        <Label htmlFor="export-preset">Preset</Label>
        <Select
          id="export-preset"
          className="w-full"
          value={preset}
          onChange={(event) => setPreset(event.target.value as ExportPreset)}
        >
          {EXPORT_PRESETS.map((candidate) => (
            <option key={candidate.value} value={candidate.value}>
              {candidate.label}
            </option>
          ))}
        </Select>
        <p className="text-xs text-muted-foreground">{chosen?.description}</p>
      </div>

      {chosen?.caveat !== undefined && (
        <Callout tone="warning" title="Before you pick this one">
          <p>{chosen.caveat}</p>
          {preset === 'campaign-audience' && listCount !== undefined && (
            <p className="mt-1">
              This list counts {listCount.toLocaleString()} contacts; the file will hold that many
              minus anyone marked do-not-contact.
            </p>
          )}
        </Callout>
      )}

      <div className="grid gap-1">
        <Label htmlFor="export-format">Format</Label>
        <Select
          id="export-format"
          className="w-full"
          value={format}
          onChange={(event) => setFormat(event.target.value as ExportFormat)}
        >
          {FORMATS.map((candidate) => (
            <option key={candidate.value} value={candidate.value}>
              {candidate.label}
            </option>
          ))}
        </Select>
        <p className="text-xs text-muted-foreground">{chosenFormat?.note}</p>
      </div>

      {isCsv ? (
        <div className="grid gap-2">
          <Label className="gap-2">
            <Checkbox
              checked={headerless}
              onCheckedChange={(checked) => setHeaderless(checked === true)}
            />
            Leave out the header row
          </Label>
          <div className="grid gap-0.5">
            <Label className="gap-2">
              <Checkbox
                checked={spreadsheetSafe}
                onCheckedChange={(checked) => setSpreadsheetSafe(checked === true)}
              />
              Safe to open in a spreadsheet
            </Label>
            <p className="pl-6 text-xs text-muted-foreground">
              Quotes cells starting with = + - @ so a spreadsheet shows them instead of running
              them. Safe to open in a spreadsheet, not safe to re-import: phone numbers like +1…
              gain a leading quote too.
            </p>
          </div>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">
          The header and spreadsheet options apply to CSV only; {chosenFormat?.label} ignores them.
        </p>
      )}

      {issues.length > 0 ? (
        <Callout tone="warning" title="Nothing to download yet">
          <ul className="list-disc space-y-0.5 pl-4">
            {issues.map((issue) => (
              <li key={issue.message}>{issue.message}</li>
            ))}
          </ul>
        </Callout>
      ) : (
        <Button
          render={
            <a href={href} download data-testid="export-download">
              Download
            </a>
          }
        />
      )}
    </div>
  )
}
