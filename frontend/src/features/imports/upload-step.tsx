import { Upload } from 'lucide-react'
import { useId, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

import { ErrorNote } from './notes'
import { MAX_IMPORT_CHARACTERS } from './read-csv'

interface UploadStepProps {
  onSelect: (file: File) => void
  pending: boolean
  pendingName: string | null
  error: string | null
}

/**
 * Step 1: choose a file (spec 10.5, P1-21).
 *
 * What happens next depends on what this is: the zip LinkedIn sends, one of
 * its CSVs read on its own, or any other CSV. `onSelect` hands the file
 * straight up without judging it — the wizard decides its shape from there and
 * says which one it found before anything is read or sent.
 */
export function UploadStep({ onSelect, pending, pendingName, error }: UploadStepProps) {
  const inputId = useId()
  const input = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  function take(files: FileList | null) {
    const file = files?.[0]
    if (file) onSelect(file)
  }

  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle level={2}>Choose a file to import</CardTitle>
          <CardDescription>
            The zip LinkedIn emails you, one of its files on its own (Connections.csv, messages.csv,
            Invitations.csv), or any other CSV with a header row. The next screen says which one
            netkeeper found before anything is imported.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div
            onDragOver={(event) => {
              event.preventDefault()
              if (!pending) setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault()
              setDragging(false)
              // The input and the button both go inert while a file is being
              // read; the drop target used to stay live regardless, so a drop
              // mid-read could race the read it interrupted (review finding
              // 5) and, either way, ignored the same "busy" state visible two
              // inches away.
              if (!pending) take(event.dataTransfer.files)
            }}
            aria-disabled={pending}
            className={cn(
              'flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-8 text-center',
              dragging ? 'border-primary bg-muted' : 'border-border',
            )}
          >
            <Upload className="size-6 text-muted-foreground" aria-hidden="true" />
            <label htmlFor={inputId} className="font-medium">
              File to import
            </label>
            <input
              ref={input}
              id={inputId}
              type="file"
              accept=".zip,.csv,application/zip,text/csv"
              disabled={pending}
              className="sr-only"
              onChange={(event) => {
                take(event.target.files)
                // Clear it so choosing the same file twice fires `change` again.
                event.target.value = ''
              }}
            />
            <Button onClick={() => input.current?.click()} disabled={pending}>
              {pending ? 'Reading…' : 'Choose a file'}
            </Button>
            <p className="text-muted-foreground">or drop one here</p>
          </div>

          {pending && pendingName && (
            <p role="status" className="text-muted-foreground">
              Reading {pendingName}…
            </p>
          )}
          {error && <ErrorNote>{error}</ErrorNote>}

          <p className="text-muted-foreground">
            A CSV is read in your browser and sent as text, up to{' '}
            {MAX_IMPORT_CHARACTERS / 1_000_000} MB of it; its encoding is worked out from its bytes,
            so an export from a Windows tool keeps its accents, and the next screen names the
            encoding used and lets you change it. A zip is sent as it was downloaded, with nothing
            unzipped in your browser. Either way, nothing leaves this machine except that one
            upload.
          </p>
        </CardContent>
      </Card>

      <GettingYourData />
    </div>
  )
}

/**
 * How to get the archive, and which file to pick from it by hand (P1-21 item 3).
 *
 * Written for someone who has never done this before. The menu path below is
 * the one this repo's own reference workflow already documents
 * (docs/networking-workflow.md); everything past "open Settings & Privacy" is
 * described by what it is for rather than by a button's exact wording, so it
 * stays true if LinkedIn moves the menu around.
 */
function GettingYourData() {
  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Getting your data from LinkedIn</CardTitle>
        <CardDescription>For anyone who has not requested this before.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-muted-foreground">
        <div className="space-y-1.5">
          <p className="font-medium text-foreground">Don&rsquo;t have the file yet?</p>
          <ol className="list-decimal space-y-1.5 pl-5">
            <li>
              In LinkedIn, go to <strong className="text-foreground">Settings &amp; Privacy</strong>
              , then <strong className="text-foreground">Data privacy</strong>, then{' '}
              <strong className="text-foreground">Get a copy of your data</strong>.
            </li>
            <li>
              Ask for your full data archive, not just Connections — netkeeper also reads your
              message and invitation history, and a connections-only export leaves both out.
            </li>
            <li>
              Request it and wait. LinkedIn emails you a download link — usually well under a day;
              budget for 1 to 24 hours.
            </li>
            <li>
              Download the zip from that email and choose it above, or drop it onto this page. There
              is nothing to unzip first.
            </li>
          </ol>
        </div>
        <div className="space-y-1.5">
          <p className="font-medium text-foreground">Already unzipped it by hand?</p>
          <p>
            Choose the whole zip if you still have it — netkeeper reads connections, messages, and
            invitations from it in one step. Picking through the extracted files yourself works too:{' '}
            <strong className="text-foreground">Connections.csv</strong> imports your contact list,
            and <strong className="text-foreground">messages.csv</strong> or{' '}
            <strong className="text-foreground">Invitations.csv</strong> each add that file&rsquo;s
            history to contacts you already have. This screen&rsquo;s archive path doesn&rsquo;t
            read anything else LinkedIn includes yet (skills, positions, education, and the like) —
            though a file like that can still be picked here and mapped by hand as a generic CSV.
          </p>
        </div>
      </CardContent>
    </Card>
  )
}
