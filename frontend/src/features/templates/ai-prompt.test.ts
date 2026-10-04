import { describe, expect, it } from 'vitest'

import type { MergeField } from './api'
import {
  answerFormat,
  buildPrompt,
  formatStep,
  lintRulesFor,
  parseReply,
  ruleApplies,
  type DraftRequest,
} from './ai-prompt'
import { WHY_IT_MATTERS } from './lint'

const REQUEST: DraftRequest = {
  goal: 'Say I am looking for a product role',
  audience: 'Former colleagues',
  tone: 'warm',
  steps: 2,
  mention: 'The offsite last spring',
}

/** As the endpoint answers with a contact picked: that contact's values in `example`. */
const FIELDS: MergeField[] = [
  {
    name: 'first_name',
    group: 'contact',
    description: "The contact's first name.",
    insert: 'first_name',
    example: 'Robin',
    example_source: 'contact',
  },
  {
    name: 'company',
    group: 'contact',
    description: "The contact's current company.",
    insert: 'company',
    example: 'Quillfeather Labs',
    example_source: 'contact',
  },
  {
    name: 'me.name',
    group: 'me',
    description: 'Your name, from [me] in the config.',
    insert: 'me.name',
    example: 'Dana Placeholder',
    example_source: 'config',
  },
  {
    name: 'previous_send_date',
    group: 'campaign',
    description: 'When the previous step went out.',
    insert: 'previous_send_date | ago',
    example: '3 weeks ago',
    example_source: 'placeholder',
  },
]

describe('buildPrompt', () => {
  it('lists every allowed field as a placeholder, with its description', () => {
    const prompt = buildPrompt(REQUEST, 'email', FIELDS)
    expect(prompt).toContain("- {{ first_name }}: The contact's first name.")
    expect(prompt).toContain("- {{ company }}: The contact's current company.")
    expect(prompt).toContain('- {{ me.name }}: Your name, from [me] in the config.')
    expect(prompt).toContain('- {{ previous_send_date | ago }}: When the previous step went out.')
    expect(prompt).toContain('Never write a real name')
  })

  it('carries no example value, so a contact picked in the preview never reaches it', () => {
    const prompt = buildPrompt(REQUEST, 'email', FIELDS)
    for (const field of FIELDS) {
      if (field.example !== null) expect(prompt).not.toContain(field.example)
    }
  })

  it('includes the form', () => {
    const prompt = buildPrompt(REQUEST, 'email', FIELDS)
    expect(prompt).toContain('What the campaign is for: Say I am looking for a product role')
    expect(prompt).toContain('Who it is for: Former colleagues')
    expect(prompt).toContain('Tone: warm')
    expect(prompt).toContain('Mention: The offsite last spring')
    expect(prompt).toContain('a sequence of 2 emails')
  })

  it('leaves out a blank form line', () => {
    const prompt = buildPrompt({ ...REQUEST, mention: '  ' }, 'email', FIELDS)
    expect(prompt).not.toContain('Mention:')
  })

  it("includes lint's own rule sentences for the channel", () => {
    const prompt = buildPrompt(REQUEST, 'email', FIELDS)
    expect(prompt).toContain(WHY_IT_MATTERS.no_contact_field)
    expect(prompt).toContain(WHY_IT_MATTERS.undefined_variable)
    expect(prompt).toContain(WHY_IT_MATTERS.missing_subject)
    expect(prompt).not.toContain(WHY_IT_MATTERS.missing_value)
  })

  it('includes the length guidance', () => {
    expect(buildPrompt(REQUEST, 'email', FIELDS)).toContain(
      'at most 120 words for the first message and 80 words for each follow-up',
    )
    expect(buildPrompt({ ...REQUEST, steps: 1 }, 'linkedin', FIELDS)).toContain(
      'at most 80 words for the first message.',
    )
  })

  it('asks for the labeled format, one block per step', () => {
    const prompt = buildPrompt(REQUEST, 'email', FIELDS)
    expect(prompt).toContain('Answer in exactly this format')
    expect(prompt).toContain('Step 1\nSubject: <one line>\nBody:')
    expect(prompt).toContain('End of step 2')
    expect(prompt).not.toContain('Step 3')
  })

  it('asks a LinkedIn message for no subject', () => {
    const prompt = buildPrompt(REQUEST, 'linkedin', FIELDS)
    expect(prompt).toContain('A LinkedIn message has no subject line.')
    expect(prompt).not.toContain('Subject: <one line>')
    expect(prompt).not.toContain(WHY_IT_MATTERS.missing_subject)
  })

  it('keeps the step count between 1 and 5', () => {
    expect(buildPrompt({ ...REQUEST, steps: 0 }, 'email', FIELDS)).toContain('one email')
    expect(buildPrompt({ ...REQUEST, steps: 9 }, 'email', FIELDS)).toContain('a sequence of 5')
  })
})

describe('ruleApplies', () => {
  it('scopes the subject rule to email and leaves the preview-only rule out', () => {
    expect(ruleApplies('missing_subject', 'email')).toBe(true)
    expect(ruleApplies('missing_subject', 'linkedin')).toBe(false)
    expect(ruleApplies('missing_value', 'email')).toBe(false)
    expect(ruleApplies('bad_link', 'linkedin')).toBe(true)
  })

  it('takes every other rule from the lint metadata', () => {
    const shared = Object.keys(WHY_IT_MATTERS).length - 2 // missing_subject, missing_value
    expect(lintRulesFor('linkedin')).toHaveLength(shared)
    expect(lintRulesFor('email')).toHaveLength(shared + 1)
  })
})

describe('parseReply', () => {
  it('reads the labeled format', () => {
    const reply = answerFormat('email', 1)
      .replace('<one line>', 'Catching up, {{ first_name }}?')
      .replace(
        '<the message, over as many lines as it needs>',
        'Hi {{ first_name }},\n\nIt has been a while.',
      )
    expect(parseReply(reply)).toEqual([
      {
        subject: 'Catching up, {{ first_name }}?',
        body: 'Hi {{ first_name }},\n\nIt has been a while.',
      },
    ])
  })

  it('ignores chatter around the format, fences, and bold labels', () => {
    const reply = [
      "Sure! Here's a draft you can use:",
      '',
      '```',
      '**Step 1**',
      '**Subject:** Quick hello',
      '**Body:**',
      'Hi {{ first_name }},',
      'Hope all is well.',
      'End of step 1',
      '```',
      '',
      'Let me know if you want it shorter.',
    ].join('\n')
    expect(parseReply(reply)).toEqual([
      { subject: 'Quick hello', body: 'Hi {{ first_name }},\nHope all is well.' },
    ])
  })

  it('reads several steps, with or without the end markers', () => {
    const reply = [
      'Step 1',
      'Subject: First',
      'Body:',
      'Hi {{ first_name }}.',
      'End of step 1',
      '',
      'Step 2',
      'Subject: Second',
      'Body: Following up, {{ first_name }}.',
      '',
      'Step 3',
      'Body:',
      'Last one, {{ first_name }}.',
    ].join('\n')
    expect(parseReply(reply)).toEqual([
      { subject: 'First', body: 'Hi {{ first_name }}.' },
      { subject: 'Second', body: 'Following up, {{ first_name }}.' },
      { subject: null, body: 'Last one, {{ first_name }}.' },
    ])
  })

  it('splits steps on a new subject when the step markers are missing', () => {
    const reply = 'Subject: One\nBody:\nA {{ first_name }}\nSubject: Two\nBody:\nB {{ first_name }}'
    expect(parseReply(reply)).toEqual([
      { subject: 'One', body: 'A {{ first_name }}' },
      { subject: 'Two', body: 'B {{ first_name }}' },
    ])
  })

  it('gives null for a reply without the labels, so the caller pastes it raw', () => {
    expect(parseReply('Hi {{ first_name }}, long time no see!')).toBeNull()
    expect(parseReply('Subject: only a subject')).toBeNull()
    expect(parseReply('')).toBeNull()
  })

  it('reads back what formatStep writes', () => {
    const step = { subject: 'Hello', body: 'Hi {{ first_name }},\n\nBye.' }
    expect(parseReply(formatStep(step, 2))).toEqual([step])
    const noSubject = { subject: null, body: 'Hi {{ first_name }}.' }
    expect(parseReply(formatStep(noSubject, 1))).toEqual([noSubject])
  })
})
