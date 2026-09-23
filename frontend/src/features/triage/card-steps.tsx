/**
 * The three steps of a card, in the order they are worked (#142).
 *
 * The met call is the point of the screen and the *last* thing to do on a card.
 * Fixing what you call somebody, and putting the tag on that you thought of
 * while reading their headline, both happen while you are looking at them — and
 * before CP2.5 the screen taught the opposite order: the decision buttons led
 * the row, and the name editor and the tag picker were overlays that only
 * existed once you had pressed `p` or `t`, under a card you had usually already
 * decided. So:
 *
 *   1. **Name** — what you call them, with the editor opening in place.
 *   2. **Tags** — the tags already on them, with the picker opening in place.
 *   3. **Have you met them?** — the loudest thing on the card, and last.
 *
 * **What did not change: the keys.** `m`, `n`, `s`, `t` and `p` still fire from
 * anywhere on the screen through the same handler, so a run by somebody who
 * knows the map costs exactly what it cost before. Only the reading order moved.
 * Nothing here is modal, nothing traps focus, and no button waits on a round
 * trip — the twelve-seconds-a-contact budget in `triage-page.tsx` rules all
 * three out.
 *
 * **The wrong-contact guard.** `open` is derived in `triage-page.tsx` from the
 * contact the editor was opened for, so an editor closes by derivation the
 * moment the card moves; `key={contact.id}` then makes React build a fresh one
 * for the next person rather than reusing the state of the last. Both halves
 * are load-bearing and neither is new: the editors moved out of the overlay
 * stack, but a name typed for one person still cannot be submitted against the
 * next. See `OpenEditor` in `triage-page.tsx`.
 *
 * This is deliberately *not* inside the card's live region. The region is
 * atomic, so a screen reader re-reads all of it whenever it changes, and a text
 * field inside one would re-read the whole card on every keystroke.
 */

import { Button } from '@/components/ui/button'

import { PreferredNameEditor } from './preferred-name-editor'
import { TagPicker } from './tag-picker'
import { DECISION_MEANINGS } from './method'
import { bindingFor } from './keymap'
import type { TriageAction } from './keymap'
import type { TriageCard as Card, TriageTag } from './api'

/** Which in-place editor is open for the contact on screen. */
export type OpenEditor = 'none' | 'name' | 'tags'

/** The key a step is reached by, printed the way the button row prints it. */
function Key({ action }: { action: TriageAction }) {
  const label = bindingFor(action)?.label
  if (label === undefined) return null
  return (
    <kbd
      aria-hidden="true"
      className="rounded border border-current/25 px-1 font-mono text-[0.7rem] leading-4 opacity-70"
    >
      {label}
    </kbd>
  )
}

function Step({
  number,
  label,
  children,
}: {
  number: number
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <span aria-hidden="true" className="text-xs tabular-nums text-muted-foreground">
        {number}
      </span>
      <span className="w-11 shrink-0 text-sm text-muted-foreground">{label}</span>
      {children}
    </div>
  )
}

export function CardSteps({
  card,
  open,
  onOpen,
  onClose,
  onRename,
  onAddTag,
  onRemoveTag,
  onAction,
}: {
  card: Card
  open: OpenEditor
  onOpen: (editor: 'name' | 'tags') => void
  onClose: () => void
  onRename: (contactId: number, preferredName: string) => Promise<void>
  onAddTag: (contactId: number, tag: TriageTag) => void
  onRemoveTag: (contactId: number, tagId: number) => void
  /** The page's own handler, so a click here is the keystroke it stands for. */
  onAction: (action: TriageAction) => void
}) {
  const { contact } = card
  const name = `${contact.preferred_name} ${contact.last_name}`.trim()

  return (
    <section
      aria-label="What to do with this contact"
      data-testid="triage-steps"
      className="flex min-w-0 flex-col gap-3 rounded-xl bg-card p-4 ring-1 ring-foreground/10"
    >
      <div className="flex flex-col gap-2">
        <Step number={1} label="Name">
          {open === 'name' ? (
            <PreferredNameEditor
              key={contact.id}
              contactId={contact.id}
              initial={contact.preferred_name}
              firstName={contact.first_name}
              onSave={onRename}
              onClose={onClose}
            />
          ) : (
            <>
              <span className="min-w-0 break-words font-medium">{contact.preferred_name}</span>
              {contact.preferred_name !== contact.first_name && (
                <span className="text-xs text-muted-foreground">
                  (given name {contact.first_name})
                </span>
              )}
              <Button
                size="xs"
                variant="outline"
                className="ml-auto"
                aria-keyshortcuts={bindingFor('preferred-name')?.aria}
                onClick={() => onOpen('name')}
              >
                Edit
                <Key action="preferred-name" />
              </Button>
            </>
          )}
        </Step>

        <Step number={2} label="Tags">
          {contact.tags.length === 0 ? (
            <span className="text-sm text-muted-foreground">None yet</span>
          ) : (
            <ul className="flex min-w-0 flex-wrap items-center gap-1">
              {contact.tags.map((tag) => (
                <li key={tag.id}>
                  <span className="inline-flex items-center gap-1 rounded-md bg-muted px-1.5 py-0.5 text-xs ring-1 ring-foreground/10">
                    {tag.name}
                    <button
                      type="button"
                      // Named for the tag, so a screen reader hears which one
                      // this takes off rather than a row of bare crosses.
                      aria-label={`Take the tag ${tag.name} off ${name}`}
                      className="rounded-sm px-0.5 leading-none opacity-60 hover:opacity-100 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
                      onClick={() => onRemoveTag(contact.id, tag.id)}
                    >
                      <span aria-hidden="true">×</span>
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          )}
          {open !== 'tags' && (
            <Button
              size="xs"
              variant="outline"
              className="ml-auto"
              aria-keyshortcuts={bindingFor('tag')?.aria}
              onClick={() => onOpen('tags')}
            >
              Add tag
              <Key action="tag" />
            </Button>
          )}
        </Step>

        {open === 'tags' && (
          <TagPicker
            key={contact.id}
            applied={contact.tags}
            onAdd={(tag) => onAddTag(contact.id, tag)}
            onRemove={(tagId) => onRemoveTag(contact.id, tagId)}
            onClose={onClose}
          />
        )}
      </div>

      <div className="border-t border-foreground/10 pt-3">
        <p id="triage-decision-question" className="font-heading font-medium">
          <span aria-hidden="true" className="mr-2 text-xs font-normal text-muted-foreground">
            3
          </span>
          Have you met {contact.preferred_name}?
        </p>
        <div
          role="group"
          aria-labelledby="triage-decision-question"
          data-testid="triage-decision"
          className="mt-2 flex flex-wrap gap-2"
        >
          {DECISION_MEANINGS.map((meaning) => (
            <Button
              key={meaning.action}
              size="sm"
              variant={meaning.action === 'skip' ? 'outline' : 'default'}
              // The button row carries the same three actions, so this one says
              // who it is about: "Met — Ada Example-1", not a second "Met".
              aria-label={`${meaning.term} — ${name}`}
              aria-keyshortcuts={bindingFor(meaning.action)?.aria}
              onClick={() => onAction(meaning.action)}
            >
              {meaning.term}
              <Key action={meaning.action} />
            </Button>
          ))}
        </div>
        <dl
          data-testid="decision-meanings"
          className="mt-2 grid grid-cols-[max-content_1fr] gap-x-2 gap-y-0.5 text-xs text-muted-foreground"
        >
          {DECISION_MEANINGS.map((meaning) => (
            <div key={meaning.action} className="contents">
              <dt className="font-medium">{meaning.term}</dt>
              <dd className="min-w-0">{meaning.short}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  )
}
