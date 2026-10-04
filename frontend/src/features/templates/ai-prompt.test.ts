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
import { PROMPT_RULES, WHY_IT_MATTERS } from './lint'

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
    name: 'title',
    group: 'contact',
    description: "The contact's current job title.",
    insert: 'title',
    example: 'Dana Placeholder',
    example_source: 'contact',
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
    expect(prompt).toContain("- {{ title }}: The contact's current job title.")
    expect(prompt).toContain('- {{ previous_send_date | ago }}: When the previous step went out.')
    expect(prompt).toContain('Never write a real name')
  })

  it('asks for no detail about me, since templates have no me.* fields (#342)', () => {
    const prompt = buildPrompt(REQUEST, 'email', FIELDS)
    expect(prompt).not.toContain('{{ me.')
    expect(prompt).toContain('Leave out my own name and signature')
    expect(prompt).not.toContain('detail about the recipient or me')
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

  it("includes lint's rules for the channel, phrased as rules", () => {
    const prompt = buildPrompt(REQUEST, 'email', FIELDS)
    expect(prompt).toContain('- Use only the merge fields listed above, spelled exactly as shown.')
    expect(prompt).toContain(`- ${PROMPT_RULES.no_contact_field}`)
    expect(prompt).toContain(`- ${PROMPT_RULES.missing_subject}`)
    // The rule sentences, not the reasons behind them.
    expect(prompt).not.toContain(WHY_IT_MATTERS.undefined_variable)
    expect(prompt).not.toContain(WHY_IT_MATTERS.missing_value)
  })

  it('has a prompt rule for every lint rule but the preview-only one', () => {
    for (const rule of Object.keys(WHY_IT_MATTERS) as (keyof typeof WHY_IT_MATTERS)[]) {
      if (rule === 'missing_value') expect(PROMPT_RULES[rule]).toBeNull()
      else expect(PROMPT_RULES[rule]).toEqual(expect.any(String))
    }
  })

  it('leaves the current text out unless asked', () => {
    const current = { subject: 'Old subject', body: 'Old body, {{ first_name }}' }
    expect(buildPrompt(REQUEST, 'email', FIELDS)).not.toContain('Current template to improve')
    const prompt = buildPrompt(REQUEST, 'email', FIELDS, current)
    expect(prompt).toContain(
      'Current template to improve:\n\nSubject: Old subject\nBody:\nOld body, {{ first_name }}',
    )
    expect(prompt.indexOf('Current template to improve')).toBeLessThan(
      prompt.indexOf('Answer in exactly this format'),
    )
    const linkedin = buildPrompt(REQUEST, 'linkedin', FIELDS, current)
    expect(linkedin).toContain('Current template to improve:\n\nBody:\nOld body')
    expect(linkedin).not.toContain('Subject: Old subject')
    expect(buildPrompt(REQUEST, 'email', FIELDS, { subject: ' ', body: '' })).not.toContain(
      'Current template to improve',
    )
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
    expect(prompt).not.toContain(PROMPT_RULES.missing_subject)
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
    const rules = Object.keys(PROMPT_RULES)
    const linkedinOnly = rules.filter((rule) => rule.startsWith('linkedin_')).length
    expect(linkedinOnly).toBe(5)
    const shared = rules.length - 2 - linkedinOnly // missing_subject, missing_value
    expect(lintRulesFor('linkedin')).toHaveLength(shared + linkedinOnly)
    expect(lintRulesFor('email')).toHaveLength(shared + 1)
  })

  it('gives a LinkedIn prompt the LinkedIn rules and an email prompt none of them', () => {
    expect(lintRulesFor('linkedin')).toContain(PROMPT_RULES.linkedin_newline)
    expect(lintRulesFor('linkedin')).toContain(PROMPT_RULES.linkedin_untypable)
    expect(lintRulesFor('email')).not.toContain(PROMPT_RULES.linkedin_subject)
    expect(answerFormat('linkedin', 1)).toContain('<the message, as one paragraph on one line>')
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
    expect(parseReply(reply)).toEqual({
      steps: [
        {
          subject: 'Catching up, {{ first_name }}?',
          body: 'Hi {{ first_name }},\n\nIt has been a while.',
        },
      ],
      dropped: 0,
    })
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
    // The two chatter lines are counted, so the editor can say they were left out.
    expect(parseReply(reply)).toEqual({
      steps: [{ subject: 'Quick hello', body: 'Hi {{ first_name }},\nHope all is well.' }],
      dropped: 2,
    })
  })

  it('reads several steps without end markers, split on blank lines', () => {
    const reply = [
      'Step 1',
      'Subject: First',
      'Body:',
      'Hi {{ first_name }}.',

      '',
      'Step 2',
      'Subject: Second',
      'Body: Following up, {{ first_name }}.',
      '',
      'Step 3',
      'Body:',
      'Last one, {{ first_name }}.',
    ].join('\n')
    expect(parseReply(reply)).toEqual({
      steps: [
        { subject: 'First', body: 'Hi {{ first_name }}.' },
        { subject: 'Second', body: 'Following up, {{ first_name }}.' },
        { subject: null, body: 'Last one, {{ first_name }}.' },
      ],
      dropped: 0,
    })
  })

  it('splits steps on a new subject after a blank line when the step markers are missing', () => {
    const reply =
      'Subject: One\nBody:\nA {{ first_name }}\n\nSubject: Two\nBody:\nB {{ first_name }}'
    expect(parseReply(reply)).toEqual({
      steps: [
        { subject: 'One', body: 'A {{ first_name }}' },
        { subject: 'Two', body: 'B {{ first_name }}' },
      ],
      dropped: 0,
    })
  })

  it('A: keeps a Subject: line inside a body when no blank line comes before it', () => {
    const reply =
      'Subject: Hello\nBody:\nHi {{ first_name }},\nSubject: the offsite photos\nare up.'
    expect(parseReply(reply)).toEqual({
      steps: [
        {
          subject: 'Hello',
          body: 'Hi {{ first_name }},\nSubject: the offsite photos\nare up.',
        },
      ],
      dropped: 0,
    })
  })

  it('B: keeps a bare Step N line inside a body when no blank line comes before it', () => {
    const reply = 'Step 1\nSubject: Hello\nBody:\nThe plan, {{ first_name }}:\nStep 2\nis lunch.'
    expect(parseReply(reply)?.steps).toEqual([
      { subject: 'Hello', body: 'The plan, {{ first_name }}:\nStep 2\nis lunch.' },
    ])
  })

  it('A and B with end markers: only End of step ends a body', () => {
    const reply = [
      'Step 1',
      'Subject: One',
      'Body:',
      'Hi {{ first_name }},',
      '',
      'Subject: the offsite photos are up.',
      '',
      'Step 2',
      'is lunch.',
      'End of step 1',
      'Step 2',
      'Subject: Two',
      'Body:',
      'B {{ first_name }}',
      'End of step 2',
    ].join('\n')
    expect(parseReply(reply)).toEqual({
      steps: [
        {
          subject: 'One',
          body: 'Hi {{ first_name }},\n\nSubject: the offsite photos are up.\n\nStep 2\nis lunch.',
        },
        { subject: 'Two', body: 'B {{ first_name }}' },
      ],
      dropped: 0,
    })
  })

  it('splits blocks under headers like Email 1 and Email 2, and drops the headers', () => {
    const reply = [
      'Email 1',
      'Subject: One',
      'Body:',
      'A {{ first_name }}',
      'Email 2',
      'Subject: Two',
      'Body:',
      'B {{ first_name }}',
      '',
      '**Email 3:**',
      'Subject: Three',
      'Body:',
      'C {{ first_name }}',
    ].join('\n')
    expect(parseReply(reply)).toEqual({
      steps: [
        { subject: 'One', body: 'A {{ first_name }}' },
        { subject: 'Two', body: 'B {{ first_name }}' },
        { subject: 'Three', body: 'C {{ first_name }}' },
      ],
      dropped: 3,
    })
  })

  it('C: counts the lines it leaves out, around and between the steps', () => {
    const reply = [
      'Here are two options.',
      'Step 1',
      'Subject: One',
      'Body:',
      'A {{ first_name }}',
      'End of step 1',
      'And a follow-up:',
      '',
      'Step 2',
      'Subject: Unused',
      'Subject: Two',
      'Body:',
      'B {{ first_name }}',
      'End of step 2',
      'Want changes?',
    ].join('\n')
    expect(parseReply(reply)).toEqual({
      steps: [
        { subject: 'One', body: 'A {{ first_name }}' },
        { subject: 'Two', body: 'B {{ first_name }}' },
      ],
      dropped: 4,
    })
  })

  it('stays fast on long lines of spaces', () => {
    for (const reply of ['Step 1' + ' '.repeat(100_000) + 'x', 'body' + ' '.repeat(100_000)]) {
      const started = performance.now()
      parseReply(reply)
      expect(performance.now() - started).toBeLessThan(50)
    }
  })

  it('gives null for a reply without the labels, so the caller pastes it raw', () => {
    expect(parseReply('Hi {{ first_name }}, long time no see!')).toBeNull()
    expect(parseReply('Subject: only a subject')).toBeNull()
    expect(parseReply('')).toBeNull()
  })

  it('reads back what formatStep writes', () => {
    const step = { subject: 'Hello', body: 'Hi {{ first_name }},\n\nBye.' }
    expect(parseReply(formatStep(step, 2))).toEqual({ steps: [step], dropped: 0 })
    const noSubject = { subject: null, body: 'Hi {{ first_name }}.' }
    expect(parseReply(formatStep(noSubject, 1))).toEqual({ steps: [noSubject], dropped: 0 })
  })
})
