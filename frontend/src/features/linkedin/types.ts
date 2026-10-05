import type { components, paths } from '@/api/schema'

/**
 * The LinkedIn page's shapes, derived from the generated client (spec 14.1,
 * P2-10's `/linkedin` resource). Nothing here is hand-written: a field the
 * backend renames or drops surfaces as a type error, not a wrong read.
 */
type JsonOf<T> = T extends { content: { 'application/json': infer Body } } ? Body : never

export type RunKind = components['schemas']['SyncRunKind']
export type RunStatus = components['schemas']['SyncRunStatus']
export type RunTrigger = components['schemas']['SyncRunTrigger']

export type Run = components['schemas']['RunOut']
export type RunPage = JsonOf<paths['/api/v1/linkedin/runs']['get']['responses'][200]>
export type RunAccepted = JsonOf<paths['/api/v1/linkedin/runs']['post']['responses'][202]>
export type RunContact = components['schemas']['RunContactOut']
export type RunContacts = components['schemas']['RunContactsOut']
export type RunDiagnostics = components['schemas']['RunDiagnosticsOut']
export type RunVisitReason = components['schemas']['RunVisitReasonOut']
export type RunLostAnswer = components['schemas']['RunLostAnswerOut']

export type Budget = components['schemas']['BudgetOut']
export type PeriodBudget = components['schemas']['PeriodBudgetOut']
export type TodaysVisits = components['schemas']['TodaysVisitsOut']
export type BudgetStatus = JsonOf<paths['/api/v1/linkedin/budget']['get']['responses'][200]>

export type Heat = JsonOf<paths['/api/v1/linkedin/heat']['get']['responses'][200]>

export type Pin = components['schemas']['PinOut']

export type ScheduledJob = components['schemas']['ScheduledJobOut']
export type Schedule = JsonOf<paths['/api/v1/linkedin/schedule']['get']['responses'][200]>

export type LinkedInStatus = JsonOf<paths['/api/v1/linkedin/status']['get']['responses'][200]>
export type SessionFlag = NonNullable<LinkedInStatus['session_flag']>

export type BrowserLaunch = JsonOf<paths['/api/v1/linkedin/browser']['get']['responses'][200]>
export type BrowserHealth = JsonOf<
  paths['/api/v1/linkedin/browser/health']['get']['responses'][200]
>

/** The run kinds the worker can actually run (`RUNNABLE_KINDS`, `netkeeper/services/runs.py`).
 *
 * `message_send` has no runner yet (spec 9.4), and the API answers a start for it
 * with `422`. `inbox` has a runner (P4-08) but no page source until P4-01, so a
 * poll can only fail. The Start dialog offers neither.
 */
export const RUNNABLE_KINDS: readonly RunKind[] = [
  'connections_full',
  'connections_incremental',
  'enrich',
]

export const RUN_KIND_LABELS: Record<RunKind, string> = {
  connections_full: 'Full connections sync',
  connections_incremental: 'Incremental connections sync',
  enrich: 'Enrichment',
  inbox: 'Inbox poll',
  message_send: 'Message send',
}

export const RUN_TRIGGER_LABELS: Record<RunTrigger, string> = {
  manual: 'Manual',
  scheduled: 'Scheduled',
}

export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  running: 'Running',
  completed: 'Completed',
  aborted: 'Aborted',
  failed: 'Failed',
}

export const ACTION_CLASS_LABELS: Record<string, string> = {
  connection_pages: 'Connection pages',
  profile_visits: 'Profile visits',
  inbox_polls: 'Inbox polls',
  li_messages_auto: 'Auto-sent LinkedIn messages',
  li_prefills: 'LinkedIn prefills',
}
