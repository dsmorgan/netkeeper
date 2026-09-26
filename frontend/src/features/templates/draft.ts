/** The editor's working copy of a template, and how it compares to the saved one. */
import type { TemplateDraft, TemplateOut } from './api'

/** How long typing has to pause before the draft is linted. */
export const LINT_DEBOUNCE_MS = 400

export function draftOf(template: TemplateOut | null): TemplateDraft {
  return {
    name: template?.name ?? '',
    channel: template?.channel ?? 'email',
    subject: template?.subject ?? '',
    body: template?.body ?? '',
  }
}

export function sameDraft(a: TemplateDraft, b: TemplateDraft): boolean {
  return (
    a.name === b.name && a.channel === b.channel && a.subject === b.subject && a.body === b.body
  )
}
