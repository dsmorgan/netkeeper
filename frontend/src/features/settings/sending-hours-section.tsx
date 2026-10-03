import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

import { type SendingHours, type SendingHoursIn, saveSendingHours, sendingHoursQuery } from './api'
import { DAYS, sendingHoursProblem } from './sending-hours'

/**
 * The sending hours (#338): when campaign email may go out. Only a campaign's start
 * ignores them; the rest of its first batch, every follow-up, retry and leftover waits
 * for them.
 */
function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function SendingHoursSection() {
  const current = useQuery(sendingHoursQuery)
  return (
    <Card size="sm">
      <CardHeader>
        <h2 className="font-heading text-sm leading-snug font-medium">Sending hours</h2>
        <CardDescription>
          When campaign email may go out, in your time zone
          {current.data !== undefined ? ` (${current.data.timezone})` : ''}. Only a campaign&apos;s
          start ignores them: its first batch goes at the start you choose, whatever the hour, and
          keeps going that day until the daily caps stop it. The rest of that batch, every
          follow-up, every retry, and anything left over while netkeeper was not running sends only
          inside these hours.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {current.isPending && <p role="status">Loading…</p>}
        {current.isError && <p role="alert">{message(current.error)}</p>}
        {current.data !== undefined && <SendingHoursForm current={current.data} />}
      </CardContent>
    </Card>
  )
}

function SendingHoursForm({ current }: { current: SendingHours }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<SendingHoursIn>({
    enabled: current.enabled,
    days: current.days,
    start: current.start,
    end: current.end,
  })
  const [saved, setSaved] = useState<string | null>(null)
  const anyTimeId = useId()
  const startId = useId()
  const endId = useId()
  const save = useMutation({
    mutationFn: saveSendingHours,
    onSuccess: (data) => {
      queryClient.setQueryData(sendingHoursQuery.queryKey, data)
      setSaved(data.summary)
    },
  })
  const problem = sendingHoursProblem(draft)
  const change = (next: Partial<SendingHoursIn>) => {
    setSaved(null)
    setDraft((d) => ({ ...d, ...next }))
  }

  return (
    <form
      aria-label="Sending hours"
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (problem === null) save.mutate(draft)
      }}
    >
      <div className="flex items-center gap-2">
        <Checkbox
          id={anyTimeId}
          checked={!draft.enabled}
          onCheckedChange={(checked) => change({ enabled: !checked })}
        />
        <Label htmlFor={anyTimeId}>Any time (no sending hours)</Label>
      </div>
      <div className="flex flex-wrap gap-3" role="group" aria-label="Days">
        {DAYS.map((day) => (
          <label key={day} className="flex items-center gap-1.5">
            <Checkbox
              checked={draft.days.includes(day)}
              disabled={!draft.enabled}
              onCheckedChange={(checked) =>
                change({
                  days: checked
                    ? DAYS.filter((d) => d === day || draft.days.includes(d))
                    : draft.days.filter((d) => d !== day),
                })
              }
            />
            {day}
          </label>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor={startId}>From</Label>
          <Input
            id={startId}
            type="time"
            value={draft.start}
            disabled={!draft.enabled}
            onChange={(event) => change({ start: event.target.value })}
            className="w-fit"
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={endId}>To</Label>
          <Input
            id={endId}
            type="time"
            value={draft.end}
            disabled={!draft.enabled}
            onChange={(event) => change({ end: event.target.value })}
            className="w-fit"
          />
        </div>
      </div>
      {problem !== null && <p role="alert">{problem}</p>}
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={problem !== null || save.isPending}>
          Save sending hours
        </Button>
        {saved !== null && (
          <span role="status" className="text-muted-foreground">
            Saved: {saved}.
          </span>
        )}
      </div>
      {save.isError && <p role="alert">{message(save.error)}</p>}
    </form>
  )
}
