/**
 * What a prefill run says to the person (#383, ADR 0007): the "typing…" note, how each
 * outcome ended, each refusal reason in plain words, and what to do about the message
 * bubble the run may have left open.
 *
 * A prefill run records its outcome as `stop_reason` (`prefilled`, `not_typed`,
 * `too_long`, `partially_typed`, `unknown`) and the reason as `error`: a fixed phrase,
 * or one of three codes (`netkeeper/linkedin/browser.py`, `page_messaging.py`,
 * `messaging.py`, `services/message_send.py`). None of them holds message text, a name,
 * or a URL. A phrase this file does not know is shown as the backend wrote it.
 *
 * This file imports nothing, so any screen can use it.
 */

/** Before you click: what Prefill does, and what you shouldn't do while it types. */
export const PREFILL_NOTE =
  'Prefill types the message into a message bubble in Chrome. The person you are writing to may see "typing…" while netkeeper types, and you click Send yourself. Don\'t type or click in Chrome until the prefill finishes.'

/** While it types. */
export const TYPING = 'Typing in Chrome… watch the netkeeper Chrome window.'
export const TYPING_WARNING =
  'The person you are writing to may see "typing…" while netkeeper types. Don\'t type or click in Chrome until the prefill finishes.'

/** The first poll hasn't run: scheduled polls wait for one you run by hand. */
export const FIRST_POLL_COMMAND = 'netkeeper linkedin inbox'
export const FIRST_POLL_NOTE = `netkeeper has not read your LinkedIn inbox yet, so LinkedIn prefills are held until it has. Run ${FIRST_POLL_COMMAND} by hand in a terminal, or use Check inbox now. Scheduled polls start after the first one.`

/** Close the bubble a refusal that followed the Message click can leave open. */
export const CLOSE_BUBBLE =
  'netkeeper opened a message bubble in Chrome and left it open. It is empty. Close it before you prefill again, because netkeeper refuses a page with more than one message bubble.'
export const MAYBE_CLOSE_BUBBLE =
  'If a message bubble is open in Chrome, close it before you prefill again. netkeeper never closes it for you.'

/** Part of the message is in the composer (ADR 0007's wording). */
export const PARTLY_TYPED =
  "Part of a message is in the composer and may be kept as a draft. Don't click Send. Clear it, or close the message bubble, which deletes the draft."

/** After the prefill typed the whole message. */
export const TYPED_WHOLE =
  'The prefill typed the message. It waits for you below: review it in Chrome and click Send yourself.'

/** A message bubble already held text, so netkeeper typed nothing. */
export const DRAFT_IN_BUBBLE =
  'A message bubble in Chrome already holds text, and netkeeper left it alone. Clear the text or close the bubble before you prefill again.'

/** The outcomes a prefill run records as `stop_reason`. */
export type PrefillOutcome = 'prefilled' | 'not_typed' | 'too_long' | 'partially_typed' | 'unknown'

const OUTCOMES: readonly string[] = [
  'prefilled',
  'not_typed',
  'too_long',
  'partially_typed',
  'unknown',
]

export function isPrefillOutcome(reason: string | null): reason is PrefillOutcome {
  return reason !== null && OUTCOMES.includes(reason)
}

/** Each outcome in a few words, for the runs list. */
export const OUTCOME_TEXT: Readonly<Record<PrefillOutcome, string>> = {
  prefilled: 'typed the message and handed the tab to you',
  not_typed: 'nothing typed',
  too_long: 'nothing typed: the message is too long to type',
  partially_typed: 'part of the message was typed',
  unknown: 'part of the message may have been typed',
}

/** Whether the refusal followed the Message click, so a bubble may be open. */
type Bubble = 'closed' | 'open' | 'maybe' | 'draft'

interface Rule {
  match: string | RegExp
  text: string | ((groups: RegExpExecArray) => string)
  bubble: Bubble
}

/** The refusals before the click: nothing was opened. */
const BEFORE_CLICK: readonly Rule[] = [
  { match: 'no claim', text: 'The run had no message to type.', bubble: 'closed' },
  {
    match: 'the claim lapsed',
    text: 'The claim on this message ran out before the prefill started.',
    bubble: 'closed',
  },
  { match: 'the message has no body', text: 'The message has no text.', bubble: 'closed' },
  {
    match: 'the contact has no usable LinkedIn URN',
    text: 'The contact has no usable LinkedIn identity.',
    bubble: 'closed',
  },
  {
    match: /^the typing plan refused the body/,
    text: "netkeeper can't type this message as written, for example because it holds a character netkeeper doesn't type. Fix the template.",
    bubble: 'closed',
  },
  {
    match: 'the body is over the typing ceiling',
    text: 'The message takes too long to type (the limit is five minutes). Shorten the template.',
    bubble: 'closed',
  },
  {
    match: 'the LinkedIn session is flagged',
    text: 'The LinkedIn session is flagged.',
    bubble: 'closed',
  },
  { match: 'heat is too high', text: 'LinkedIn heat is too high.', bubble: 'closed' },
  {
    match: "today's LinkedIn budget is spent",
    text: "Today's LinkedIn budget is spent.",
    bubble: 'closed',
  },
  {
    match: 'the contact has no public profile id to open',
    text: "The contact has no public LinkedIn profile address, so netkeeper can't open the profile.",
    bubble: 'closed',
  },
  {
    match: /^the page answered (.+)$/,
    text: (m) =>
      `LinkedIn answered with a ${(m[1] ?? '').replace(/_/g, ' ')} page instead of the profile.`,
    bubble: 'closed',
  },
  {
    match: 'the profile opened somewhere else',
    text: 'The profile address opened a different page.',
    bubble: 'closed',
  },
  {
    match: 'no Message control on the page',
    text: 'The profile has no Message button.',
    bubble: 'closed',
  },
  {
    match: "the tab is not on the contact's profile",
    text: "The tab wasn't on the contact's profile, so netkeeper didn't click Message.",
    bubble: 'closed',
  },
  {
    match: 'the tab left the profile before the click',
    text: "The tab left the contact's profile before netkeeper clicked Message.",
    bubble: 'closed',
  },
  {
    match: 'the Message control could not be read',
    text: "netkeeper couldn't read the Message button, so it didn't click it.",
    bubble: 'closed',
  },
  {
    match: 'no Message control is visible',
    text: "The profile's Message button isn't visible, so netkeeper didn't click it.",
    bubble: 'closed',
  },
  { match: 'the browser was busy', text: 'Chrome was busy with another run.', bubble: 'closed' },
  {
    match: 'the browser was unavailable',
    text: "netkeeper couldn't reach Chrome.",
    bubble: 'closed',
  },
  {
    match: "a Message control opens something other than this contact's compose",
    text: "A Message button on the profile doesn't open a message to this contact.",
    bubble: 'closed',
  },
]

/** The refusals that follow the click, and the ones that can't say. */
const AFTER_CLICK: readonly Rule[] = [
  {
    match: 'the Message control was already clicked',
    text: 'netkeeper had already clicked Message in this run.',
    bubble: 'maybe',
  },
  {
    match: 'the Message control could not be clicked',
    text: "Chrome didn't take netkeeper's click on Message.",
    bubble: 'maybe',
  },
  {
    match: 'the Message control was not clicked',
    text: "netkeeper didn't click Message.",
    bubble: 'maybe',
  },
  {
    match: 'recipient_name_mismatch',
    text: "The name in the message bubble doesn't match the name on the profile.",
    bubble: 'open',
  },
  {
    match: 'recipient_name_unreadable',
    text: "netkeeper couldn't read the recipient's name in the message bubble, so it couldn't check it against the profile.",
    bubble: 'open',
  },
  {
    match: 'another_compose',
    text: "A second message composer opened while netkeeper was checking, so it couldn't tell which one it would type into.",
    bubble: 'open',
  },
  {
    match: /^no compose option was loaded/,
    text: "LinkedIn didn't answer the Message click.",
    bubble: 'open',
  },
  {
    match: /compose option/,
    text: "LinkedIn's answer to the Message click wasn't what netkeeper expected, so it didn't type.",
    bubble: 'open',
  },
  {
    match: 'the bubble was not drawn',
    text: "The message bubble didn't open in time.",
    bubble: 'open',
  },
  {
    match: /more than one message (composer|bubble)|there is more than one/,
    text: 'More than one message bubble is open in Chrome.',
    bubble: 'open',
  },
  {
    match: /for someone else/,
    text: 'The message bubble is for someone else.',
    bubble: 'open',
  },
  {
    match: /bubble|composer is not in|recipient/,
    text: "The message bubble didn't look the way netkeeper expects for this contact, so it didn't type.",
    bubble: 'open',
  },
  { match: "the tab's url changed", text: 'The tab moved to another page.', bubble: 'open' },
  {
    match: 'the tab or the browser went away',
    text: 'The tab or Chrome closed.',
    bubble: 'maybe',
  },
  {
    match: 'the composer is not empty',
    text: 'The message box already holds text, so netkeeper left it alone.',
    bubble: 'draft',
  },
  {
    match: "the composer's text changed",
    text: 'The text in the message box changed while netkeeper typed.',
    bubble: 'open',
  },
  {
    match: 'the composer does not hold focus',
    text: 'The message box lost focus, so netkeeper stopped.',
    bubble: 'open',
  },
  {
    match: 'the composer could not be read',
    text: "netkeeper couldn't read the message box, so it stopped.",
    bubble: 'open',
  },
  { match: 'a key call failed', text: 'Chrome rejected a keystroke.', bubble: 'open' },
  { match: 'cancelled', text: 'You cancelled the run.', bubble: 'maybe' },
  {
    match: 'interrupted',
    text: 'netkeeper stopped while the prefill ran.',
    bubble: 'maybe',
  },
  {
    match: /^the prefill failed \((.+)\)$/,
    text: (m) => `The prefill hit an error (${m[1] ?? ''}).`,
    bubble: 'maybe',
  },
  {
    match: /^no tab with a clicked Message control|^the plan /,
    text: "netkeeper couldn't start typing.",
    bubble: 'maybe',
  },
]

const RULES: readonly Rule[] = [...BEFORE_CLICK, ...AFTER_CLICK]

export interface PrefillReason {
  /** The reason in plain words, or the backend's own phrase when it has none. */
  text: string
  bubble: Bubble
}

const AFTER_TYPING = 'after typing: '

/** One recorded reason in plain words, and whether it can leave a bubble open. */
export function prefillReason(error: string | null): PrefillReason {
  if (error === null || error === '') return { text: 'No reason was recorded.', bubble: 'maybe' }
  const afterTyping = error.startsWith(AFTER_TYPING)
  const phrase = afterTyping ? error.slice(AFTER_TYPING.length) : error
  for (const rule of RULES) {
    const groups =
      typeof rule.match === 'string'
        ? phrase === rule.match
          ? ([phrase] as unknown as RegExpExecArray)
          : null
        : rule.match.exec(phrase)
    if (groups === null) continue
    const text = typeof rule.text === 'string' ? rule.text : rule.text(groups)
    return { text: afterTyping ? `After typing, ${lowerFirst(text)}` : text, bubble: rule.bubble }
  }
  return { text: error, bubble: 'maybe' }
}

function lowerFirst(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1)
}

/** What a finished prefill run tells you, ready to show. */
/**
 * Whether a bubble may be open, from what the run recorded (`message_click_attempted`
 * and `message_clicked` in its counts): no click, no bubble; a click that landed, an
 * open one; a click that was sent but raised, maybe. Without both, `fallback`.
 */
export function bubbleFromCounts(
  counts: Readonly<Record<string, unknown>> | null,
  fallback: Bubble,
): Bubble {
  const attempted = counts?.message_click_attempted
  const clicked = counts?.message_clicked
  if (typeof attempted !== 'boolean' || typeof clicked !== 'boolean') return fallback
  if (!attempted) return 'closed'
  if (fallback === 'draft') return 'draft'
  return clicked ? 'open' : 'maybe'
}

export interface PrefillEnding {
  title: string
  reason: string | null
  /** What to do next, one sentence each. */
  steps: string[]
}

/**
 * How a prefill that did not prefill ended. `not_typed` and `too_long` typed nothing;
 * `partially_typed` and `unknown` may have left text in the composer. Null for an
 * outcome that is not a prefill's (a flagged session, an error): the run's own stop
 * reason says those.
 */
export function prefillEnding(
  stopReason: string | null,
  error: string | null,
  counts: Readonly<Record<string, unknown>> | null = null,
): PrefillEnding | null {
  if (!isPrefillOutcome(stopReason) || stopReason === 'prefilled') return null
  const why = prefillReason(error)
  if (stopReason === 'partially_typed' || stopReason === 'unknown') {
    return {
      title:
        stopReason === 'partially_typed'
          ? 'The prefill stopped part of the way through typing.'
          : "The prefill stopped, and netkeeper can't tell how much it typed.",
      reason: why.text,
      steps: [PARTLY_TYPED, 'netkeeper never clears the composer and never retries a prefill.'],
    }
  }
  const steps: string[] = []
  if (stopReason === 'too_long') {
    steps.push('netkeeper parked the enrollment, so it waits for you. Shorten the template first.')
  }
  // The run's own record of the click wins; the phrase table is for older runs.
  const bubble = bubbleFromCounts(counts, why.bubble)
  if (bubble === 'draft') steps.push(DRAFT_IN_BUBBLE)
  if (bubble === 'open') steps.push(CLOSE_BUBBLE)
  if (bubble === 'maybe') steps.push(MAYBE_CLOSE_BUBBLE)
  return {
    title: 'Not prefilled. Nothing was typed in Chrome.',
    reason: why.text,
    steps,
  }
}
