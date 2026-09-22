import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'

import { createRun, importKeys, inspectFile, presetsQuery, runQuery, savePreset } from './api'
import { type ArchiveKind, archiveKindOf } from './archive-kind'
import { ArchiveImportFlow } from './archive-flow'
import { DraftReview } from './draft-review'
import { asColumnMapping } from './fields'
import { MappingStep } from './mapping-step'
import { ErrorNote } from './notes'
import { type Decoded, type Encoding, readCsvFile } from './read-csv'
import { StepNav } from './step-nav'
import { UploadStep } from './upload-step'
import type { ColumnMapping, ImportField, ImportRun, Inspection } from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** The mapping the wizard sends: every header, with `''` for the ones left out. */
function mappingOfRun(run: ImportRun): ColumnMapping {
  return run.mapping as ColumnMapping
}

/**
 * The import wizard: upload, map, preview, decide, commit (spec 10.5).
 *
 * `resumeRunId` picks up a draft that was read but never committed, which is
 * what the history page links to; everything else starts from a file.
 */
export function ImportWizard({ resumeRunId }: { resumeRunId?: number }) {
  if (resumeRunId !== undefined) {
    return <ResumeDraft runId={resumeRunId} />
  }
  return <NewImport />
}

function ResumeDraft({ runId }: { runId: number }) {
  const run = useQuery(runQuery(runId))
  const [restarted, setRestarted] = useState(false)

  if (restarted) return <NewImport />
  if (run.isPending) {
    return <p role="status">Loading the import…</p>
  }
  if (run.isError) {
    return <ErrorNote>{message(run.error)}</ErrorNote>
  }
  // Whether the run is still a draft is DraftReview's to judge, and it judges
  // it after its own commit. Deciding here instead meant that committing —
  // which invalidates this very query — brought the run back as `committed` and
  // replaced the result of the commit with "already committed".
  return (
    <DraftReview
      run={run.data}
      mapping={mappingOfRun(run.data)}
      onBackToMapping={null}
      onRestart={() => setRestarted(true)}
    />
  )
}

function NewImport() {
  // The File itself is kept so a different encoding can be tried without
  // asking for the file again; `file` is the text it decoded to.
  const [chosen, setChosen] = useState<File | null>(null)
  const [file, setFile] = useState<Decoded | null>(null)
  const [inspection, setInspection] = useState<Inspection | null>(null)
  const [presetChoice, setPresetChoice] = useState<string | null>(null)
  const [mapping, setMapping] = useState<ColumnMapping>({})
  const [run, setRun] = useState<ImportRun | null>(null)
  const [savedPreset, setSavedPreset] = useState<string | null>(null)
  // The archive shape (spec 10.5, P1-21): a zip, or a lone `messages.csv` /
  // `Invitations.csv`, neither of which goes through `open` below at all —
  // `archiveKindOf` decides which shape a chosen file gets before anything
  // is read. `Connections.csv` on its own is not one of these; it keeps going
  // through the CSV shape, which is the only one that can review a candidate.
  const [archive, setArchive] = useState<{ file: File; kind: ArchiveKind } | null>(null)
  const queryClient = useQueryClient()

  // A draft already read with this exact mapping, so stepping back to the
  // mapping screen and forward again does not leave a second draft behind.
  const lastRead = useRef<{ signature: string; run: ImportRun } | null>(null)

  const presets = useQuery(presetsQuery)

  const open = useMutation({
    mutationFn: async ({ picked, as }: { picked: File; as?: Encoding }) => {
      const read = await readCsvFile(picked, as)
      if (!read.ok) throw new Error(read.reason)
      return { picked, read, inspected: await inspectFile({ content: read.content }) }
    },
    onSuccess: ({ picked, read, inspected }) => {
      setChosen(picked)
      setFile(read)
      setInspection(inspected)
      // A different encoding can change the headers themselves, so the mapping
      // is taken from the file as just read rather than carried over.
      setPresetChoice(inspected.preset)
      setMapping(asColumnMapping(inspected.headers, inspected.mapping))
      setSavedPreset(null)
      lastRead.current = null
    },
  })

  const reinspect = useMutation({
    mutationFn: async (choice: { name: string | null; mapping: ColumnMapping | null }) => {
      if (file === null) throw new Error('no file is open')
      return inspectFile({
        content: file.content,
        preset: choice.mapping === null ? choice.name : null,
        mapping: choice.mapping,
      })
    },
    onSuccess: (inspected, choice) => {
      setInspection(inspected)
      setPresetChoice(choice.name)
      setMapping(asColumnMapping(inspected.headers, inspected.mapping))
    },
  })

  const read = useMutation({
    mutationFn: async () => {
      if (file === null) throw new Error('no file is open')
      return createRun({
        filename: file.filename,
        content: file.content,
        preset: presetChoice,
        mapping,
      })
    },
    onSuccess: (created) => {
      lastRead.current = { signature: signatureOf(presetChoice, mapping), run: created }
      setRun(created)
      void queryClient.invalidateQueries({ queryKey: importKeys.all })
    },
  })

  const keepPreset = useMutation({
    mutationFn: (name: string) => savePreset(name, mappedOnly(mapping)),
    onSuccess: (_saved, name) => {
      setSavedPreset(name)
      void queryClient.invalidateQueries({ queryKey: importKeys.presets() })
    },
  })

  function choosePreset(name: string | null) {
    setSavedPreset(null)
    if (name === null) {
      // "By hand" keeps what is on screen as a starting point; nothing to ask.
      setPresetChoice(null)
      return
    }
    const saved = presets.data?.saved.find((preset) => preset.name === name)
    if (saved === undefined) {
      reinspect.mutate({ name, mapping: null })
      return
    }
    // `/imports/inspect` only knows the built-in presets, so a saved one is sent
    // as an explicit mapping over every header: the ones it does not name are
    // unmapped rather than left on whatever the detected preset guessed.
    const headers = inspection?.headers ?? []
    const explicit: ColumnMapping = {}
    for (const header of headers) {
      explicit[header] = (saved.mapping[header] as ImportField | undefined) ?? ''
    }
    reinspect.mutate({ name, mapping: explicit })
  }

  if (archive !== null) {
    return (
      <ArchiveImportFlow
        file={archive.file}
        kind={archive.kind}
        onBack={() => setArchive(null)}
        onRestart={() => setArchive(null)}
      />
    )
  }

  if (run !== null && file !== null) {
    return (
      <DraftReview
        run={run}
        mapping={mapping}
        onBackToMapping={() => setRun(null)}
        onRestart={() => {
          setChosen(null)
          setFile(null)
          setInspection(null)
          setRun(null)
          setMapping({})
          setPresetChoice(null)
          lastRead.current = null
        }}
      />
    )
  }

  if (inspection !== null && file !== null) {
    return (
      <div className="flex flex-col gap-4">
        <StepNav current="mapping" />
        <MappingStep
          filename={file.filename}
          encoding={file.encoding}
          encodingReason={file.reason}
          replacements={file.replacements}
          damagedUtf8={file.damagedUtf8}
          utf8Damage={file.utf8Damage}
          characters={file.content.length}
          onEncodingChange={(next) => {
            if (chosen !== null) open.mutate({ picked: chosen, as: next })
          }}
          inspection={inspection}
          presets={presets.data}
          presetChoice={presetChoice}
          mapping={mapping}
          onPresetChange={choosePreset}
          onFieldChange={(header, field) => {
            setMapping((current) => ({ ...current, [header]: field }))
            // The mapping is the person's own now; claiming a preset's name for
            // it would put the wrong label on the run and its history.
            setPresetChoice(null)
            setSavedPreset(null)
          }}
          onSavePreset={(name) => keepPreset.mutate(name)}
          savingPreset={keepPreset.isPending}
          savedPreset={savedPreset}
          savePresetError={keepPreset.isError ? message(keepPreset.error) : null}
          onBack={() => {
            setChosen(null)
            setFile(null)
            setInspection(null)
            lastRead.current = null
          }}
          onContinue={() => {
            const cached = lastRead.current
            if (cached !== null && cached.signature === signatureOf(presetChoice, mapping)) {
              setRun(cached.run)
              return
            }
            read.mutate()
          }}
          pending={read.isPending || reinspect.isPending || open.isPending}
          error={
            // `open` is on this screen too: the encoding select re-reads the
            // file through it, and leaving it out made that fail in silence.
            read.isError
              ? message(read.error)
              : reinspect.isError
                ? message(reinspect.error)
                : open.isError
                  ? message(open.error)
                  : null
          }
        />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <StepNav current="upload" />
      <UploadStep
        onSelect={(picked) => {
          const kind = archiveKindOf(picked.name)
          if (kind !== null) {
            setArchive({ file: picked, kind })
            return
          }
          open.mutate({ picked })
        }}
        pending={open.isPending}
        pendingName={open.variables?.picked.name ?? null}
        error={open.isError ? message(open.error) : null}
      />
    </div>
  )
}

function signatureOf(preset: string | null, mapping: ColumnMapping): string {
  return JSON.stringify([preset, Object.entries(mapping).sort()])
}

/** Only the columns that feed a field; a preset never stores the blanks. */
function mappedOnly(mapping: ColumnMapping): Record<string, string> {
  return Object.fromEntries(Object.entries(mapping).filter(([, field]) => field !== ''))
}
