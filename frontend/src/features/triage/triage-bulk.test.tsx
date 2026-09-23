/**
 * The bulk suggestion banner (spec 10.2).
 *
 * The count on the banner is a preview, and the apply sends it back. When the
 * set has moved the API refuses with `409` and writes nothing — which is not a
 * failure to report but a preview to take again, and that is what this asserts.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { createFakeBackend, type FakeBackend } from './test-backend'
import { currentName, renderTriage } from './test-render'
import type { Tag } from './api'

/**
 * A tag the user has said means "I have not met these people".
 *
 * The only `not_met` batch there is, now that nothing decides `not_met` from an
 * absence (#142): a tag's meaning is the user's own declared rule rather than
 * something netkeeper worked out from a quiet message history.
 */
const RECRUITER: Tag = {
  id: 9,
  name: 'recruiter',
  color: null,
  kind: 'auto',
  met_signal: 'not_met',
  contact_count: 3,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const RECRUITER_BATCH = `tag:${RECRUITER.id}`

/**
 * Six contacts: three with message history, three carrying the recruiter tag.
 *
 * The tag is `auto` — a rule put it there, not a hand — so what puts those
 * three in the batch can only be the meaning the user gave the tag. A `manual`
 * tag would have tripped "you tagged them yourself" as well, and a test that
 * two things can pass proves neither.
 */
function withARecruiterTag(): FakeBackend {
  const backend = createFakeBackend({ contacts: 6, withMessages: 3, tags: [RECRUITER] })
  for (const id of [4, 5, 6]) {
    backend
      .byId(id)
      .tags.push({ id: RECRUITER.id, name: RECRUITER.name, color: null, kind: 'auto' })
  }
  return backend
}

/** The banner row for one batch, which is how a test says which it means. */
function bannerFor(key: string): HTMLElement {
  const row = document.querySelector(`[data-key="${key}"]`)
  if (row === null) throw new Error(`no banner for ${key}`)
  return row as HTMLElement
}

async function findBannerFor(key: string): Promise<HTMLElement> {
  await waitFor(() => expect(document.querySelector(`[data-key="${key}"]`)).not.toBeNull())
  return bannerFor(key)
}

describe('the bulk suggestion', () => {
  it('is not drawn when it matches nobody', async () => {
    renderTriage({ contacts: 4, withMessages: 0 })
    await currentName()

    await waitFor(() => expect(document.querySelector('[data-key="met_with_messages"]')).toBeNull())
  })

  it('previews with a count and applies as one batch', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()

    const banner = await findBannerFor('met_with_messages')
    expect(banner).toHaveTextContent('You have message threads with 3 untriaged people.')

    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(backend.byId(2).met).toBe('met')
    expect(backend.byId(3).met).toBe('met')
    expect(backend.byId(4).met).toBe('unknown')
    expect(await screen.findByRole('status')).toHaveTextContent(/Marked 3 contacts as met/)
    // One batch id, so one undo takes it all back.
    const batches = new Set(backend.decisions.map((decision) => decision.batch_id))
    expect(batches.size).toBe(1)
  })

  it('goes away once everyone it matched has an answer', async () => {
    renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))

    await waitFor(() => expect(document.querySelector('[data-key="met_with_messages"]')).toBeNull())
  })

  it('re-previews instead of erroring when the count moved', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await screen.findByRole('button', { name: 'Mark 3 as met' })

    // A fourth person gains message history after the banner was drawn.
    backend.setMessageCount(4, 2)
    fireEvent.click(screen.getByRole('button', { name: 'Mark 3 as met' }))

    // Nothing was applied, and the banner comes back with the count as it is.
    expect(await screen.findByRole('button', { name: 'Mark 4 as met' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/nothing was applied/i)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    for (const id of [1, 2, 3, 4]) expect(backend.byId(id).met).toBe('unknown')

    // Accepting the new count applies it.
    fireEvent.click(screen.getByRole('button', { name: 'Mark 4 as met' }))
    await waitFor(() => expect(backend.byId(4).met).toBe('met'))
  })

  it('is taken back whole by one undo', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))
    await waitFor(() => expect(backend.byId(3).met).toBe('met'))

    fireEvent.keyDown(window, { key: 'u' })

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
    expect(backend.byId(2).met).toBe('unknown')
    expect(backend.byId(3).met).toBe('unknown')
    expect(await findBannerFor('met_with_messages')).toHaveTextContent('3 untriaged people')
  })

  it('moves the queue on when the batch empties the front of it', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    expect(await currentName()).toContain('Ada')

    fireEvent.click(await screen.findByRole('button', { name: 'Mark 3 as met' }))

    await waitFor(() => expect(backend.byId(3).met).toBe('met'))
    await waitFor(async () => expect(await currentName()).toContain('Dev'))
  })
})

describe('what netkeeper decided (P1-28)', () => {
  /** Accepts the offered batch, which is what leaves decisions to review. */
  async function acceptTheBatch() {
    fireEvent.click(await screen.findByRole('button', { name: /^Mark \d+ as met$/ }))
    await screen.findByRole('status')
  }

  it('says how many are waiting, and opens the queue that walks them', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await acceptTheBatch()

    const prompt = await screen.findByTestId('automatic-pass')
    expect(prompt).toHaveTextContent('netkeeper decided 3 contacts from a batch you accepted')

    // An answer of the person's own, in a state the review queue also serves:
    // it must stay out of it, which is the whole point of `decided_by`.
    fireEvent.keyDown(window, { key: 'n' })
    await waitFor(() => expect(backend.byId(4).met).toBe('not_met'))
    expect(backend.byId(4).met_source).toBe('manual')

    fireEvent.click(screen.getByRole('button', { name: 'Review them' }))

    // The queue now serves the contacts the batch decided, not the untriaged
    // and not the one answered by hand.
    await waitFor(async () => expect(await currentName()).toContain('Ada'))
    expect(backend.byId(1).met).toBe('met')
    const ahead = screen.getByTestId('triage-queue-list')
    expect(ahead).not.toHaveTextContent('Dev Testerly-4')
    expect(screen.getByRole('button', { name: 'Reviewing' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByTestId('automatic-pass')).toHaveTextContent(
      'are the 3 contacts netkeeper decided for you, waiting to be checked',
    )
  })

  it('offers no batch while reviewing, because a batch never reaches an answer', async () => {
    // The service refuses `met`/`not_met` outright (422), so asking at all
    // would be a request this screen knows better than to send.
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await acceptTheBatch()
    fireEvent.click(await screen.findByRole('button', { name: 'Review them' }))
    await waitFor(async () => expect(await currentName()).toContain('Ada'))

    expect(screen.queryAllByTestId('bulk-suggestion')).toEqual([])
    const asked = backend.seen.filter(
      (request) =>
        request.path === '/api/v1/triage/suggestions' &&
        request.search.getAll('states').includes('met'),
    )
    expect(asked).toEqual([])
  })

  it('takes a contact out of the review queue when you answer it yourself', async () => {
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await acceptTheBatch()
    fireEvent.click(await screen.findByRole('button', { name: 'Review them' }))
    await waitFor(async () => expect(await currentName()).toContain('Ada'))

    // `m` on the card in front: the same answer, now the person's own.
    fireEvent.keyDown(window, { key: 'm' })
    await waitFor(() => expect(backend.byId(1).met_source).toBe('manual'))
    expect(backend.byId(1).met).toBe('met')
    await waitFor(() =>
      expect(screen.getByTestId('automatic-pass')).toHaveTextContent(
        'are the 2 contacts netkeeper decided for you',
      ),
    )
    // And the queue moved on to the next one still waiting to be checked.
    expect(await currentName()).toContain('Bo')
  })

  it("serves the batch's work and then stops, never an answer of your own", async () => {
    // The card itself comes from `/triage/next`, which narrows on who decided.
    // Without that the queue would run on into the contacts answered by hand —
    // they are in the same two states — and the review pass would never end.
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await acceptTheBatch()
    fireEvent.keyDown(window, { key: 'n' })
    await waitFor(() => expect(backend.byId(4).met).toBe('not_met'))

    fireEvent.click(await screen.findByRole('button', { name: 'Review them' }))
    const seen: string[] = []
    for (let index = 0; index < 3; index += 1) {
      seen.push(await currentName())
      fireEvent.keyDown(window, { key: 'm' })
      await waitFor(() => expect(backend.byId(index + 1).met_source).toBe('manual'))
    }

    expect(await screen.findByText('Nothing left to review.')).toBeInTheDocument()
    expect(seen.join(' ')).not.toContain('Dev')
    expect(backend.byId(4).met_source).toBe('manual')
  })

  it('refills the review queue from the same narrowed queue it opened on', async () => {
    // `→` is the path that asks `/triage/next` again mid-run. The first cards
    // arrive with the load and every decision answers with its own successor,
    // so a refill that forgot who decided is invisible until somebody moves on
    // without deciding — and then the review pass quietly runs on into the
    // answers they gave themselves.
    const { backend } = renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await acceptTheBatch()
    fireEvent.keyDown(window, { key: 'n' })
    await waitFor(() => expect(backend.byId(4).met).toBe('not_met'))

    fireEvent.click(await screen.findByRole('button', { name: 'Review them' }))
    await waitFor(async () => expect(await currentName()).toContain('Ada'))
    for (const name of ['Bo', 'Cleo']) {
      fireEvent.keyDown(window, { key: 'ArrowRight' })
      await waitFor(async () => expect(await currentName()).toContain(name))
    }

    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(await screen.findByText('Nothing left to review.')).toBeInTheDocument()
  })

  it('says what each batch decides, not what the first one does', async () => {
    // Two batches are offered at once and they decide opposite things. A
    // button that assumed "met" read right for one of them and lied about the
    // other — and the one it lied about is the one somebody most wants to read
    // carefully before taking.
    const backend = withARecruiterTag()
    renderTriage({ backend })
    await currentName()

    const messaged = await findBannerFor('met_with_messages')
    const tagged = bannerFor(RECRUITER_BATCH)
    expect(within(messaged).getByRole('button', { name: /^Mark/ })).toHaveTextContent(
      'Mark 3 as met',
    )
    expect(within(tagged).getByRole('button', { name: /^Mark/ })).toHaveTextContent(
      'Mark 3 as not met',
    )
    expect(tagged).toHaveTextContent('which you have said means you have not met them')

    fireEvent.click(within(tagged).getByRole('button', { name: 'Mark 3 as not met' }))

    await waitFor(() => expect(backend.byId(4).met).toBe('not_met'))
    expect(backend.byId(1).met).toBe('unknown')
    expect(await screen.findByRole('status')).toHaveTextContent(/Marked 3 contacts as not met/)
  })

  it('offers no batch at all about a contact nothing is known of (#142)', async () => {
    // The shape the removed `not_met_no_evidence` batch existed to sweep up:
    // six contacts, three of them with nothing on file at all. The screen has
    // one offer, and it is about the three it has evidence for.
    renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await findBannerFor('met_with_messages')

    const banners = screen.getAllByTestId('bulk-suggestion')
    expect(banners.map((banner) => banner.dataset.key)).toEqual(['met_with_messages'])
    expect(document.body).not.toHaveTextContent(/nothing on file/i)
  })

  it('reviews what a not-met batch decided, not only a met one', async () => {
    // The review pass serves both states a batch can leave behind. Every other
    // test here reviews a `met` batch, so dropping `not_met` from the queue's
    // states would have left the suite green and the people netkeeper decided
    // *not met* unreviewable — the half of the pass most worth checking.
    const backend = withARecruiterTag()
    renderTriage({ backend })
    await currentName()

    fireEvent.click(
      within(await findBannerFor(RECRUITER_BATCH)).getByRole('button', {
        name: 'Mark 3 as not met',
      }),
    )
    await waitFor(() => expect(backend.byId(4).met).toBe('not_met'))
    expect(backend.byId(4).met_source).toBe('automatic')

    fireEvent.click(await screen.findByRole('button', { name: 'Review them' }))

    await waitFor(async () => expect(await currentName()).toContain('Dev'))
    expect(screen.getByTestId('automatic-pass')).toHaveTextContent(
      'are the 3 contacts netkeeper decided for you',
    )
    const ahead = screen.getByTestId('triage-queue-list')
    expect(ahead).toHaveTextContent('Esme Fictional-5')
    expect(ahead).not.toHaveTextContent('Ada Example-1')
  })

  it('says nothing at all until a batch has been accepted', async () => {
    renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await findBannerFor('met_with_messages')

    expect(screen.queryByTestId('automatic-pass')).not.toBeInTheDocument()
  })

  it('names the contacts a batch covers before it is applied', async () => {
    renderTriage({ contacts: 6, withMessages: 3 })
    await currentName()
    await findBannerFor('met_with_messages')

    fireEvent.click(within(bannerFor('met_with_messages')).getByRole('button', { name: 'See who' }))

    const names = await screen.findByTestId('suggestion-contacts')
    expect(names).toHaveTextContent('Ada Example-1')
    expect(names).toHaveTextContent('Cleo Placeholder-3')
    expect(names).not.toHaveTextContent('Dev Testerly-4')
  })
})
