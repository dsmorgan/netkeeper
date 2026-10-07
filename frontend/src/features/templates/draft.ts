/** The editor's working copy of a template, and how it compares to the saved one. */
import type { TemplateDraft, TemplateOut } from './api'

/** How long typing has to pause before the draft is linted. */
export const LINT_DEBOUNCE_MS = 400

export function draftOf(template: TemplateOut | null): TemplateDraft {
  const channel = template?.channel ?? 'email'
  return {
    name: template?.name ?? '',
    channel,
    // A LinkedIn message has no subject, so a template from before that rule opens without one.
    subject: channel === 'linkedin' ? '' : (template?.subject ?? ''),
    body: template?.body ?? '',
  }
}

export function sameDraft(a: TemplateDraft, b: TemplateDraft): boolean {
  return (
    a.name === b.name && a.channel === b.channel && a.subject === b.subject && a.body === b.body
  )
}
