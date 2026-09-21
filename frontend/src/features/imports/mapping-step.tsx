import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { FIELD_LABELS, IMPORT_FIELDS, duplicateScalarFields, unmappedHeaders } from './fields'
import { ErrorNote, Note } from './notes'
import {
  ENCODINGS,
  ENCODING_LABELS,
  ENCODING_REASONS,
  type Encoding,
  type EncodingReason,
} from './read-csv'
import { INPUT_CLASS, SELECT_CLASS } from './styles'
import type { ColumnMapping, ImportField, Inspection, PresetList } from './types'

interface MappingStepProps {
  filename: string
  encoding: Encoding
  encodingReason: EncodingReason
  /** Characters the decoder could not make sense of; above zero the guess is wrong. */
  replacements: number
  /** The fallback fired on what is really a UTF-8 file with a few bad bytes. */
  damagedUtf8: boolean
  /** Characters a UTF-8 read would have lost, when `damagedUtf8`. */
  utf8Damage: number
  /** Characters in the file, to say how few the damaged ones are. */
  characters: number
  onEncodingChange: (encoding: Encoding) => void
  inspection: Inspection
  presets: PresetList | undefined
  presetChoice: string | null
  mapping: ColumnMapping
  onPresetChange: (name: string | null) => void
  onFieldChange: (header: string, field: ImportField | '') => void
  onSavePreset: (name: string) => void
  savingPreset: boolean
  savedPreset: string | null
  savePresetError: string | null
  onBack: () => void
  onContinue: () => void
  pending: boolean
  error: string | null
}

function quoted(names: readonly string[]): string {
  return names.map((name) => `“${name}”`).join(', ')
}

/**
 * Step 2: which column feeds which field.
 *
 * The preset the file's header matched is named outright and can be swapped for
 * any other, and a column the mapping leaves out is called out rather than
 * quietly dropped (spec 10.5 step 2).
 */
export function MappingStep({
  filename,
  encoding,
  encodingReason,
  replacements,
  damagedUtf8,
  utf8Damage,
  characters,
  onEncodingChange,
  inspection,
  presets,
  presetChoice,
  mapping,
  onPresetChange,
  onFieldChange,
  onSavePreset,
  savingPreset,
  savedPreset,
  savePresetError,
  onBack,
  onContinue,
  pending,
  error,
}: MappingStepProps) {
  const [presetName, setPresetName] = useState('')
  const headers = inspection.headers
  const sample = inspection.sample[0]
  const unmapped = unmappedHeaders(headers, mapping)
  const duplicates = duplicateScalarFields(mapping)
  const mappedCount = headers.length - unmapped.length
  const detected = inspection.detected_preset

  return (
    <div className="flex max-w-4xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Map the columns</CardTitle>
          <CardDescription>
            {filename} · {inspection.row_count} data {inspection.row_count === 1 ? 'row' : 'rows'} ·{' '}
            {headers.length} columns
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Note>
            <p>
              {detected === null
                ? 'No built-in preset matches these columns, so start from “Map the columns by hand”.'
                : `The ${detected} preset fits this header best.`}
            </p>
            {detected !== null && presetChoice !== detected && (
              <p>
                You are using{' '}
                {presetChoice === null ? 'a mapping of your own' : `the ${presetChoice} preset`}{' '}
                instead.
              </p>
            )}
          </Note>

          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="import-encoding" className="font-medium">
              Read as
            </label>
            <select
              id="import-encoding"
              className={SELECT_CLASS}
              value={encoding}
              disabled={pending}
              onChange={(event) => onEncodingChange(event.target.value as Encoding)}
            >
              {ENCODINGS.map((candidate) => (
                <option key={candidate} value={candidate}>
                  {ENCODING_LABELS[candidate]}
                </option>
              ))}
            </select>
            <span className="text-muted-foreground">{ENCODING_REASONS[encodingReason]}</span>
          </div>

          {replacements > 0 && (
            <Note tone="warn">
              <p>
                {replacements} {replacements === 1 ? 'character' : 'characters'} could not be read
                as {ENCODING_LABELS[encoding]} and came through as &ldquo;&#xFFFD;&rdquo;. Names
                imported this way stay wrong, so try another encoding above before going on.
              </p>
            </Note>
          )}

          {encodingReason === 'fallback' &&
            (damagedUtf8 ? (
              <Note tone="warn">
                <p className="font-medium">
                  {utf8Damage} of {characters.toLocaleString()} characters are not valid UTF-8.
                </p>
                <p>
                  That is a UTF-8 file with a few damaged bytes rather than a{' '}
                  {ENCODING_LABELS['windows-1252']} one, so reading it this way mangles every accent
                  in it. Choose UTF-8 above to keep the rest and lose only the damaged characters,
                  or repair the file and start again.
                </p>
              </Note>
            ) : (
              <Note tone="warn">
                <p>
                  This file is not UTF-8, so it was read as {ENCODING_LABELS['windows-1252']} — what
                  Excel and older Windows tools write. Check an accented name in the table below; if
                  it looks wrong, pick another encoding.
                </p>
              </Note>
            ))}

          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="import-preset" className="font-medium">
              Preset
            </label>
            <select
              id="import-preset"
              className={SELECT_CLASS}
              value={presetChoice ?? ''}
              disabled={pending}
              onChange={(event) => onPresetChange(event.target.value || null)}
            >
              <option value="">Map the columns by hand</option>
              {presets && presets.builtin.length > 0 && (
                <optgroup label="Built-in">
                  {presets.builtin.map((preset) => (
                    <option key={preset.name} value={preset.name}>
                      {preset.name}
                      {preset.name === detected ? ' (detected)' : ''}
                    </option>
                  ))}
                </optgroup>
              )}
              {presets && presets.saved.length > 0 && (
                <optgroup label="Saved">
                  {presets.saved.map((preset) => (
                    <option key={preset.name} value={preset.name}>
                      {preset.name}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
            <span className="text-muted-foreground">
              {mappedCount} of {headers.length} columns mapped
            </span>
          </div>

          {inspection.preamble_rows > 0 && (
            <Note>
              <p>
                {inspection.preamble_rows} {inspection.preamble_rows === 1 ? 'line' : 'lines'} above
                the header were skipped, as in the LinkedIn archive&rsquo;s notes block.
              </p>
            </Note>
          )}

          {unmapped.length > 0 && (
            <Note tone="warn">
              <p>
                {unmapped.length} {unmapped.length === 1 ? 'column is' : 'columns are'} not
                imported: {quoted(unmapped)}.
              </p>
              <p>
                Their cells are kept on the import row so the file can be audited, but nothing is
                written to a contact. Give each one a field below to change that.
              </p>
            </Note>
          )}

          {duplicates.length > 0 && (
            <Note tone="warn">
              <p>
                More than one column feeds{' '}
                {duplicates.map((field) => FIELD_LABELS[field]).join(', ')}. The last of those
                columns in the file wins.
              </p>
            </Note>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Columns</CardTitle>
        </CardHeader>
        <CardContent>
          <table className="w-full text-left">
            <thead className="text-muted-foreground">
              <tr>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Column
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  First value
                </th>
                <th scope="col" className="py-1 font-medium">
                  Feeds
                </th>
              </tr>
            </thead>
            <tbody>
              {headers.map((header) => {
                const field = mapping[header] ?? ''
                return (
                  <tr key={header} className="border-t border-border/60">
                    <th scope="row" className="py-1.5 pr-3 font-normal">
                      {header}
                    </th>
                    <td className="max-w-48 truncate py-1.5 pr-3 text-muted-foreground">
                      {sample?.[header]?.trim() || '—'}
                    </td>
                    <td className="py-1.5">
                      <div className="flex items-center gap-2">
                        <select
                          aria-label={`Field for column ${header}`}
                          className={SELECT_CLASS}
                          value={field}
                          disabled={pending}
                          onChange={(event) =>
                            onFieldChange(header, event.target.value as ImportField | '')
                          }
                        >
                          <option value="">Not imported</option>
                          {IMPORT_FIELDS.map((candidate) => (
                            <option key={candidate} value={candidate}>
                              {FIELD_LABELS[candidate]}
                            </option>
                          ))}
                        </select>
                        {!field && (
                          <span className="text-amber-700 dark:text-amber-300">Not imported</span>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Save this mapping</CardTitle>
          <CardDescription>
            A saved preset is offered the next time a file has these columns.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="preset-name" className="font-medium">
              Preset name
            </label>
            <input
              id="preset-name"
              className={INPUT_CLASS}
              value={presetName}
              placeholder="my-crm-export"
              disabled={savingPreset}
              onChange={(event) => setPresetName(event.target.value)}
            />
            <Button
              variant="outline"
              disabled={savingPreset || presetName.trim() === '' || mappedCount === 0}
              onClick={() => onSavePreset(presetName.trim())}
            >
              {savingPreset ? 'Saving…' : 'Save preset'}
            </Button>
          </div>
          {savedPreset && <p role="status">Saved as {savedPreset}.</p>}
          {savePresetError && <ErrorNote>{savePresetError}</ErrorNote>}
        </CardContent>
      </Card>

      {error && <ErrorNote>{error}</ErrorNote>}

      <div className="flex items-center gap-2">
        <Button variant="outline" onClick={onBack} disabled={pending}>
          Choose another file
        </Button>
        <Button onClick={onContinue} disabled={pending || mappedCount === 0}>
          {pending ? 'Reading the file…' : 'Preview the import'}
        </Button>
        {mappedCount === 0 && (
          <span className="text-muted-foreground">Map at least one column first.</span>
        )}
      </div>
    </div>
  )
}
