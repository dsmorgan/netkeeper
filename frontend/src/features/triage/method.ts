/**
 * What the triage screen is for, in the method's own words.
 *
 * Every line here is lifted from `docs/networking-workflow.md`, stage 1
 * ("Validate your network"), which is the document this screen automates. It
 * lives in one module because the explainer above the card and the one-line
 * definition beside each decision button say the same thing at two lengths, and
 * a screen that explained itself twice in two different sets of words would be
 * worse than one that did not explain itself at all.
 *
 * The wording matters more than it looks. The first real run of this screen
 * produced "I don't actually know what you want me to decide here", and the
 * answer the method gives is narrow on purpose: *met* is a fact about whether a
 * conversation happened, not a judgement about whether the person is worth
 * something to you. Nothing here may drift towards the second reading.
 */

import type { TriageAction } from './keymap'

/** Why this screen exists at all, which is the first line of the explainer. */
export const TRIAGE_GOAL =
  'Decide who in your network you have actually met. Everything downstream runs on that list.'

export interface DecisionMeaning {
  /** The key-map action this explains, so the two cannot drift apart. */
  readonly action: TriageAction
  /** The word on the button. */
  readonly term: string
  /** One line, for beside the button. */
  readonly short: string
  /** The whole of it, for the explainer. */
  readonly long: string
}

/** The three decisions, defined, in the order the buttons draw them. */
export const DECISION_MEANINGS: readonly DecisionMeaning[] = [
  {
    action: 'met',
    term: 'Met',
    short: 'You have spoken with them for real. Once counts.',
    long:
      'You have spoken with them in person, on a video call, or in a real conversation. ' +
      'If you have met them once, they count. Do not weigh seniority, usefulness, or how ' +
      'long ago it was.',
  },
  {
    action: 'not-met',
    term: 'Not met',
    short: 'A connection you have never actually spoken with.',
    long:
      'A connection you have never actually spoken with. It is what is left over after you ' +
      'mark the people you have met, so netkeeper never decides it from a quiet message ' +
      'history — only you know.',
  },
  {
    action: 'skip',
    term: 'Skip',
    short: 'Unsure. They come back under Skipped.',
    long:
      'You are not sure. The contact comes back under the Skipped filter, because no decision ' +
      'is better than a guess on a list everything else runs on.',
  },
]

const BY_ACTION = new Map(DECISION_MEANINGS.map((meaning) => [meaning.action, meaning]))

/** What one decision means, or `undefined` for an action that is not a decision. */
export function meaningOf(action: TriageAction): DecisionMeaning | undefined {
  return BY_ACTION.get(action)
}
