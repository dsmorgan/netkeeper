/**
 * The campaign builder (P3-11a): name, mailbox, ordered steps, and where the
 * audience comes from. Saving makes a `draft`; nobody is enrolled until you
 * enroll the audience on the campaign's page, where the guards decide who joins.
 *
 * Each step's fields start at spec 11.2's defaults (the first at once, each
 * later one seven days on and only if nobody replied, an email follow-up in the
 * first email's thread) and are sent explicitly, so what you see is what is saved.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { ArrowDown, ArrowUp, Trash2 } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Callout } from '@/features/crm/controls'
import { stepOptionsQuery } from '@/features/linkedin-steps/api'
import { mailboxListQuery } from '@/features/mailboxes/api'
import { templatesQuery, type TemplateOut } from '@/features/templates/api'
import { LintList } from '@/features/templates/lint-list'

import {
  campaignKeys,
  createCampaign,
  errorText,
  type StepCondition,
  type StepIn,
  type StepMode,
} from './api'
import { sourceBody, sourceProblem, type AudienceSource } from './audience'
import { AudiencePicker } from './audience-picker'
import { AUTO_SEND_RISK, CONDITION_LABELS, MODE_LABELS } from './format'

const MAX_STEPS = 10

const MAILBOX_BLOCKED: Record<string, string> = {
  reauth_required: 'needs reauth',
  disabled: 'disabled',
}

interface StepDraft {
  key: number
  templateId: number | null
  delayDays: number
  mode: StepMode | null
  condition: StepCondition
  sameThread: boolean
}

const EMAIL_MODES: StepMode[] = ['draft', 'send']
/** `auto_send` is offered only while `[campaigns] linkedin_auto_send` is on (ADR 0004). */
const LINKEDIN_MODES: StepMode[] = ['prefill']
const LINKEDIN_MODES_WITH_AUTO: StepMode[] = ['prefill', 'auto_send']

function channelOf(step: StepDraft, templates: readonly TemplateOut[]) {
  return templates.find((t) => t.id === step.templateId)?.channel ?? null
}

/** Whether an email step at `index` has an earlier email step to follow up in. */
function hasEarlierEmail(steps: StepDraft[], index: number, templates: readonly TemplateOut[]) {
  return steps.slice(0, index).some((s) => channelOf(s, templates) === 'email')
}

let nextKey = 1

function newStep(index: number): StepDraft {
  return {
    key: nextKey++,
    templateId: null,
    delayDays: index === 0 ? 0 : 7,
    mode: null,
    condition: index === 0 ? 'always' : 'no_reply',
    sameThread: false,
  }
}

export function CampaignBuilderPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const templates = useQuery(templatesQuery)
  const mailboxes = useQuery(mailboxListQuery)
  const options = useQuery(stepOptionsQuery)
  const autoSend = options.data?.auto_send === true
  const [name, setName] = useState('')
  const [mailboxId, setMailboxId] = useState<number | null>(null)
  const [steps, setSteps] = useState<StepDraft[]>(() => [newStep(0)])
  const [source, setSource] = useState<AudienceSource>({ kind: 'none' })

  const templateRows = (templates.data ?? []).filter((t) => t.current)
  const connected = mailboxes.data ?? []
  const anyEmail = steps.some((s) => channelOf(s, templateRows) === 'email')

  const update = (index: number, patch: Partial<StepDraft>) =>
    setSteps((current) => current.map((s, i) => (i === index ? { ...s, ...patch } : s)))
  const move = (index: number, by: -1 | 1) =>
    setSteps((current) => {
      const next = [...current]
      next.splice(index + by, 0, ...next.splice(index, 1))
      return next
    })

  const problems: string[] = []
  if (name.trim() === '') problems.push('Give the campaign a name.')
  if (steps.some((s) => s.templateId === null)) problems.push('Pick a template for every step.')
  if (anyEmail && mailboxId === null) problems.push('Email steps need a mailbox.')
  const audienceProblem = sourceProblem(source)
  if (audienceProblem !== null) problems.push(audienceProblem)

  const save = useMutation({
    mutationFn: () => {
      const body: StepIn[] = steps.map((s, index) => {
        const channel = channelOf(s, templateRows)
        const mode = s.mode ?? (channel === 'linkedin' ? 'prefill' : 'draft')
        return {
          template_id: s.templateId as number,
          delay_days: s.delayDays,
          mode,
          condition: s.condition,
          same_thread:
            channel === 'email' && hasEarlierEmail(steps, index, templateRows) && s.sameThread,
        }
      })
      return createCampaign({
        name: name.trim(),
        mailbox_id: anyEmail ? mailboxId : null,
        steps: body,
        ...sourceBody(source),
      })
    },
    onSuccess: async (campaign) => {
      await queryClient.invalidateQueries({ queryKey: campaignKeys.all })
      void navigate({
        to: '/campaigns/$campaignId',
        params: { campaignId: String(campaign.id) },
      })
    },
  })

  return (
    <form
      className="flex max-w-4xl flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault()
        if (problems.length === 0) save.mutate()
      }}
    >
      <Card>
        <CardHeader>
          <CardTitle level={2}>New campaign</CardTitle>
          <CardDescription>
            Saved as a draft. Nothing is sent until you walk through the review and activate it.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="campaign-name">Name</Label>
            <Input
              id="campaign-name"
              value={name}
              maxLength={200}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="campaign-mailbox">Mailbox</Label>
            <Select
              id="campaign-mailbox"
              className="w-fit"
              value={mailboxId === null ? '' : String(mailboxId)}
              onChange={(event) =>
                setMailboxId(event.target.value === '' ? null : Number(event.target.value))
              }
            >
              <option value="">
                {mailboxes.isPending ? 'Loading mailboxes…' : 'No mailbox (LinkedIn steps only)'}
              </option>
              {connected.map((m) => (
                // Only an `ok` mailbox can send; the server refuses the others too.
                <option key={m.id} value={m.id} disabled={m.status !== 'ok'}>
                  {m.email}
                  {m.status === 'ok' ? '' : ` (${MAILBOX_BLOCKED[m.status]})`}
                </option>
              ))}
            </Select>
            {mailboxes.isSuccess && !connected.some((m) => m.status === 'ok') && (
              <p className="text-sm text-muted-foreground">
                No mailbox can send. Connect or reauthorize Gmail in Settings to use email steps.
              </p>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle level={2}>Steps</CardTitle>
          <CardDescription>
            In order. A step&apos;s channel is its template&apos;s. Delays count from when the
            previous step was actually sent.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {templates.isSuccess && templateRows.length === 0 && (
            <Callout tone="warning">Write a template first, on the Templates page.</Callout>
          )}
          <ol className="flex flex-col gap-3">
            {steps.map((step, index) => {
              const channel = channelOf(step, templateRows)
              const modes =
                channel === 'linkedin'
                  ? autoSend
                    ? LINKEDIN_MODES_WITH_AUTO
                    : LINKEDIN_MODES
                  : EMAIL_MODES
              const mode = step.mode !== null && modes.includes(step.mode) ? step.mode : modes[0]
              const lint = templateRows.find((t) => t.id === step.templateId)?.lint ?? []
              const threadable = channel === 'email' && hasEarlierEmail(steps, index, templateRows)
              const n = index + 1
              return (
                <li
                  key={step.key}
                  aria-label={`Step ${n}`}
                  className="grid gap-3 rounded-lg border p-3 sm:grid-cols-[auto_1fr]"
                >
                  <span className="font-medium tabular-nums">{n}</span>
                  <div className="flex flex-wrap items-end gap-3">
                    <div className="flex flex-col gap-1">
                      <Label htmlFor={`step-${step.key}-template`}>Template</Label>
                      <Select
                        id={`step-${step.key}-template`}
                        value={step.templateId === null ? '' : String(step.templateId)}
                        onChange={(event) => {
                          const id = event.target.value === '' ? null : Number(event.target.value)
                          const picked = templateRows.find((t) => t.id === id)
                          update(index, {
                            templateId: id,
                            mode: null,
                            sameThread:
                              picked?.channel === 'email' &&
                              hasEarlierEmail(steps, index, templateRows),
                          })
                        }}
                      >
                        <option value="">Pick a template</option>
                        {templateRows.map((t) => (
                          <option key={t.id} value={t.id}>
                            {t.name} ({t.channel === 'email' ? 'email' : 'LinkedIn'})
                          </option>
                        ))}
                      </Select>
                    </div>
                    <div className="flex flex-col gap-1">
                      <Label htmlFor={`step-${step.key}-delay`}>Delay (days)</Label>
                      <Input
                        id={`step-${step.key}-delay`}
                        type="number"
                        min={0}
                        max={365}
                        className="w-20"
                        value={step.delayDays}
                        onChange={(event) =>
                          update(index, {
                            delayDays: Math.min(365, Math.max(0, Number(event.target.value) || 0)),
                          })
                        }
                      />
                    </div>
                    <div className="flex flex-col gap-1">
                      <Label htmlFor={`step-${step.key}-mode`}>Mode</Label>
                      <Select
                        id={`step-${step.key}-mode`}
                        value={mode}
                        onChange={(event) =>
                          update(index, { mode: event.target.value as StepMode })
                        }
                      >
                        {modes.map((m) => (
                          <option key={m} value={m}>
                            {MODE_LABELS[m]}
                          </option>
                        ))}
                      </Select>
                    </div>
                    <div className="flex flex-col gap-1">
                      <Label htmlFor={`step-${step.key}-condition`}>Condition</Label>
                      <Select
                        id={`step-${step.key}-condition`}
                        value={step.condition}
                        onChange={(event) =>
                          update(index, { condition: event.target.value as StepCondition })
                        }
                      >
                        {(['always', 'no_reply'] as const).map((c) => (
                          <option key={c} value={c}>
                            {CONDITION_LABELS[c]}
                          </option>
                        ))}
                      </Select>
                    </div>
                    {threadable && (
                      <label className="flex h-8 items-center gap-2 text-sm">
                        <Checkbox
                          checked={step.sameThread}
                          onCheckedChange={(checked) =>
                            update(index, { sameThread: checked === true })
                          }
                        />
                        Same thread
                      </label>
                    )}
                    <div className="ml-auto flex gap-1">
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Move step ${n} up`}
                        disabled={index === 0}
                        onClick={() => move(index, -1)}
                      >
                        <ArrowUp />
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Move step ${n} down`}
                        disabled={index === steps.length - 1}
                        onClick={() => move(index, 1)}
                      >
                        <ArrowDown />
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Remove step ${n}`}
                        disabled={steps.length === 1}
                        onClick={() => setSteps((current) => current.filter((_, i) => i !== index))}
                      >
                        <Trash2 />
                      </Button>
                    </div>
                  </div>
                  {lint.length > 0 && (
                    <div className="flex flex-col gap-1 sm:col-start-2">
                      <p className="text-xs text-muted-foreground">
                        The template&apos;s lint findings. Fix them on the Templates page.
                      </p>
                      <LintList issues={lint} label={`Step ${n} template lint`} />
                    </div>
                  )}
                  {channel === 'linkedin' && mode === 'auto_send' && (
                    <Callout tone="warning" className="sm:col-start-2" title="Auto-send">
                      <p>{AUTO_SEND_RISK}</p>
                    </Callout>
                  )}
                </li>
              )
            })}
          </ol>
          <Button
            type="button"
            variant="outline"
            className="w-fit"
            disabled={steps.length >= MAX_STEPS}
            onClick={() => setSteps((current) => [...current, newStep(current.length)])}
          >
            Add a step
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle level={2}>Audience</CardTitle>
          <CardDescription>
            Who the campaign is for. You enroll them on the campaign&apos;s page, where the guards
            decide who joins and say who they left out and why.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <AudiencePicker value={source} onChange={setSource} />
        </CardContent>
      </Card>

      {save.isError && (
        <Callout tone="danger" title="The campaign was not saved.">
          <p>{errorText(save.error)}</p>
        </Callout>
      )}
      <div className="flex items-center gap-3">
        <Button type="submit" disabled={problems.length > 0 || save.isPending}>
          {save.isPending ? 'Saving…' : 'Save draft'}
        </Button>
        {problems.length > 0 && (
          <p className="text-sm text-muted-foreground">{problems.join(' ')}</p>
        )}
      </div>
    </form>
  )
}
