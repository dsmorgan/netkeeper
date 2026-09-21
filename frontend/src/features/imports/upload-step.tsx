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

/** Step 1: choose a CSV. The file is read here and sent as text (spec 10.5). */
export function UploadStep({ onSelect, pending, pendingName, error }: UploadStepProps) {
  const inputId = useId()
  const input = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  function take(files: FileList | null) {
    const file = files?.[0]
    if (file) onSelect(file)
  }

  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle>Choose a CSV</CardTitle>
        <CardDescription>
          A LinkedIn Connections export, a nine-column export, or any CSV with a header row. The
          next screen shows which column feeds which field before anything is stored.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault()
            setDragging(false)
            take(event.dataTransfer.files)
          }}
          className={cn(
            'flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-8 text-center',
            dragging ? 'border-primary bg-muted' : 'border-border',
          )}
        >
          <Upload className="size-6 text-muted-foreground" aria-hidden="true" />
          <label htmlFor={inputId} className="font-medium">
            CSV file
          </label>
          <input
            ref={input}
            id={inputId}
            type="file"
            accept=".csv,text/csv"
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
          The file is read in your browser and sent as text, up to{' '}
          {MAX_IMPORT_CHARACTERS / 1_000_000} MB of it. Its encoding is worked out from its bytes,
          so an export from a Windows tool keeps its accents; the next screen names the encoding
          used and lets you change it. Nothing leaves this machine.
        </p>
      </CardContent>
    </Card>
  )
}
