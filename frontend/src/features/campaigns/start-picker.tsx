/**
 * Choosing a campaign's scheduled start (#338): a date and time, "Now" in one click,
 * the suggestion, a warning (never a refusal) outside the suggested slots, and the
 * reminder that netkeeper sends only while `serve` runs on an awake Mac.
 */
import { useQuery } from '@tanstack/react-query'
import { useEffect, useId } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Callout } from '@/features/crm/controls'

import { startOptionsQuery } from './api'
import { formatStart, fromLocalInput, toLocalInput } from './format'
import type { StartChoice } from './start'

/** Text with `code` spans, as the backend writes the reminder. */
function withCode(text: string) {
  return text
    .split('`')
    .map((part, index) =>
      index % 2 === 1 ? <code key={index}>{part}</code> : <span key={index}>{part}</span>,
    )
}

export function StartPicker({
  campaignId,
  choice,
  onChange,
  useDefault = true,
}: {
  campaignId: number
  choice: StartChoice
  onChange: (choice: StartChoice) => void
  /** Fill in the default start once it loads, when nothing is chosen yet. */
  useDefault?: boolean
}) {
  const inputId = useId()
  const defaults = useQuery(startOptionsQuery(campaignId, null))
  const at = choice.now ? null : fromLocalInput(choice.value)
  const checked = useQuery({ ...startOptionsQuery(campaignId, at), enabled: at !== null })
  const defaultStart = defaults.data?.default_start

  useEffect(() => {
    if (useDefault && !choice.now && choice.value === '' && defaultStart !== undefined) {
      onChange({ now: false, value: toLocalInput(defaultStart) })
    }
  }, [useDefault, choice, defaultStart, onChange])

  const warning = at === null ? null : (checked.data?.warning ?? null)

  return (
    <div className="flex flex-col gap-2 text-foreground">
      <p className="font-medium" aria-live="polite">
        Starts {choice.now ? 'now' : at === null ? '—' : formatStart(at)}
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor={inputId}>Start date and time</Label>
          <Input
            id={inputId}
            type="datetime-local"
            value={choice.now ? '' : choice.value}
            onChange={(event) => onChange({ now: false, value: event.target.value })}
            className="w-fit"
          />
        </div>
        <Button
          type="button"
          variant={choice.now ? 'default' : 'outline'}
          aria-pressed={choice.now}
          onClick={() => onChange({ now: true, value: choice.value })}
        >
          Now
        </Button>
        {defaultStart !== undefined && (
          <Button
            type="button"
            variant="ghost"
            onClick={() => onChange({ now: false, value: toLocalInput(defaultStart) })}
          >
            {formatStart(defaultStart)} (default)
          </Button>
        )}
      </div>
      {defaults.data !== undefined && (
        <p className="text-muted-foreground">{defaults.data.suggestion}</p>
      )}
      {warning !== null && (
        <Callout tone="warning" title="Not a suggested time">
          <p>{warning}</p>
        </Callout>
      )}
      {defaults.data !== undefined && (
        <p className="text-muted-foreground">{withCode(defaults.data.reminder)}</p>
      )}
      {defaults.data !== undefined && (
        <p className="text-muted-foreground">{defaults.data.sending_hours}</p>
      )}
    </div>
  )
}
