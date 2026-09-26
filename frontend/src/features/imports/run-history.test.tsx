import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import {
  ARCHIVE_RESULT,
  COMMITTED_ROWS,
  COMMITTED_RUN,
  DRAFT_RUN,
  type Call,
  backend,
} from './test-support'

const ROLLED_BACK_RUN = {
  ...COMMITTED_RUN,
  id: 9,
  filename: 'older.csv',
  status: 'rolled_back' as const,
  rolled_back_at: '2026-09-20T11:00:00Z',
}

const HISTORY_DRAFT = { ...DRAFT_RUN, id: 8, filename: 'half-finished.csv' }

function historyBackend(
  runs = [COMMITTED_RUN, HISTORY_DRAFT, ROLLED_BACK_RUN],
  calls: Call[] = [],
) {
  return backend(
    { 'GET /api/v1/imports': () => jsonResponse({ items: runs, total: runs.length }) },
    calls,
  )
}

describe('import history', () => {
  it('says so plainly when nothing has been imported yet', async () => {
    mockFetch(historyBackend([]))
    await renderApp('/imports/runs')

    expect(await screen.findByText('No imports yet')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Import a CSV' })).toHaveAttribute('href', '/imports')
  })

  it('shows a loading state, then every run with its status', async () => {
    mockFetch(historyBackend())
    await renderApp('/imports/runs')

    expect(within(screen.getByRole('main')).getByRole('status')).toHaveTextContent(
      'Loading the import history…',
    )

    const row = (await screen.findByRole('link', { name: 'connections.csv' })).closest('tr')
    expect(row).not.toBeNull()
    expect(within(row as HTMLElement).getByText('Committed')).toBeVisible()
    expect(within(row as HTMLElement).getByText('2 new, 1 matched, 1 skipped')).toBeVisible()
    expect(screen.getByRole('link', { name: 'connections.csv' })).toHaveAttribute(
      'href',
      '/imports/runs/7',
    )
    expect(screen.getByText('Draft')).toBeVisible()
    expect(screen.getByText('Rolled back')).toBeVisible()
  })

  it('reports a backend that will not answer', async () => {
    mockFetch(backend({ 'GET /api/v1/imports': () => jsonResponse({ detail: 'nope' }, 500) }))
    await renderApp('/imports/runs')

    expect(await screen.findByRole('alert')).toHaveTextContent('nope')
  })
})

/**
 * As the real backend behaves: a rollback that lands flips the run to
 * `rolled_back`, which the page refetches. What it removed has to survive that.
 */
function detailBackend(
  run = COMMITTED_RUN,
  calls: Call[] = [],
  rollback?: (call: Call) => Response | undefined,
) {
  let undone = false
  return backend(
    {
      [`GET /api/v1/imports/${run.id}`]: () =>
        jsonResponse(
          undone ? { ...run, status: 'rolled_back', rolled_back_at: '2026-09-20T11:30:00Z' } : run,
        ),
      [`GET /api/v1/imports/${run.id}/rows`]: () =>
        jsonResponse({ items: COMMITTED_ROWS, total: COMMITTED_ROWS.length }),
      [`POST /api/v1/imports/${run.id}/rollback`]: (call) => {
        const refused = rollback?.(call)
        if (refused !== undefined) return refused
        undone = true
        return jsonResponse({
          run_id: run.id,
          contacts_deleted: 2,
          contacts_restored: 1,
          fields_restored: 3,
          children_deleted: 5,
        })
      },
    },
    calls,
  )
}

describe('one import run', () => {
  it('shows what the run did, including the fields provenance refused', async () => {
    mockFetch(detailBackend())
    await renderApp('/imports/runs/7')

    expect(await screen.findByText('connections.csv')).toBeVisible()
    expect(screen.getByText('Committed')).toBeVisible()
    expect(screen.getByText('Contacts created')).toBeVisible()

    expect(
      await screen.findByText(
        /Company refused: kept “Nimbus Kettle Co” from manual, not “Nimbus Kettle Works” from the file/,
      ),
    ).toBeVisible()
    expect(screen.getByText('contact #31')).toBeVisible()
  })

  it('spells out what a rollback removes before asking, and reports what it removed', async () => {
    const calls: Call[] = []
    mockFetch(detailBackend(COMMITTED_RUN, calls))
    await renderApp('/imports/runs/7')

    expect(await screen.findByText('Undo this import')).toBeVisible()
    expect(screen.getByText(/The 2 contacts this import created are deleted/)).toBeVisible()
    expect(screen.getByText(/keeps the newer value. This is not a general undo/)).toBeVisible()

    // Nothing is sent until the dialog is confirmed.
    fireEvent.click(screen.getByRole('button', { name: 'Roll back this import' }))
    const dialog = within(await screen.findByRole('alertdialog'))
    expect(dialog.getByText(/This deletes the 2 contacts the import created/)).toBeVisible()
    expect(calls.some((call) => call.method === 'POST')).toBe(false)

    // This caller never passes ConfirmDialog a confirmVariant (R-08): a
    // rollback is genuinely destructive, so it must render that way by the
    // component's own default, not by this caller opting in. `bg-destructive/10`
    // is the destructive variant's own class (button.tsx) — not the generic
    // `aria-invalid:*-destructive*` classes every button carries regardless
    // of variant, and not `default`'s `bg-primary`.
    const confirmButton = dialog.getByRole('button', {
      name: 'Delete 2 contacts and restore the rest',
    })
    expect(confirmButton.className).toContain('bg-destructive/10')
    expect(confirmButton.className).not.toContain('bg-primary')

    fireEvent.click(confirmButton)

    expect(await screen.findByText('Rolled back')).toBeVisible()
    expect(within(screen.getByRole('main')).getByRole('status')).toHaveTextContent(
      'Deleted 2 contacts this import created, put 3 fields back on 1 contact it had changed, ' +
        'and removed 5 related records such as emails, phones and positions.',
    )
    expect(calls.filter((call) => call.path === '/api/v1/imports/7/rollback')).toHaveLength(1)

    // The run has flipped to rolled_back underneath; the summary is what the
    // person came for and must not be swept away by that refetch.
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Roll back this import' })).toBeNull()
    })
    expect(within(screen.getByRole('main')).getByRole('status')).toHaveTextContent(
      'Deleted 2 contacts this import created',
    )
  })

  it('sends nothing when the confirmation is dismissed', async () => {
    const calls: Call[] = []
    mockFetch(detailBackend(COMMITTED_RUN, calls))
    await renderApp('/imports/runs/7')

    fireEvent.click(await screen.findByRole('button', { name: 'Roll back this import' }))
    const dialog = within(await screen.findByRole('alertdialog'))
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }))

    expect(calls.some((call) => call.method === 'POST')).toBe(false)
    expect(screen.getByRole('button', { name: 'Roll back this import' })).toBeVisible()
  })

  it('shows a rollback that simply failed without closing the dialog', async () => {
    mockFetch(
      detailBackend(COMMITTED_RUN, [], () => jsonResponse({ detail: 'database is locked' }, 500)),
    )
    await renderApp('/imports/runs/7')

    fireEvent.click(await screen.findByRole('button', { name: 'Roll back this import' }))
    const dialog = within(await screen.findByRole('alertdialog'))
    fireEvent.click(dialog.getByRole('button', { name: 'Delete 2 contacts and restore the rest' }))

    // Worth another try, so the dialog stays where it is with the reason in it.
    expect(await screen.findByRole('alert')).toHaveTextContent('database is locked')
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('explains a rollback the backend refuses because of a later merge', async () => {
    const detail =
      'this run created contact(s) 31, 32, which a merge has since drawn in; rolling it back ' +
      'would delete rows the merge moved onto them, or leave behind rows it moved off them. ' +
      'Undo the merge first.'
    mockFetch(detailBackend(COMMITTED_RUN, [], () => jsonResponse({ detail }, 409)))
    await renderApp('/imports/runs/7')

    fireEvent.click(await screen.findByRole('button', { name: 'Roll back this import' }))
    const dialog = within(await screen.findByRole('alertdialog'))
    fireEvent.click(dialog.getByRole('button', { name: 'Delete 2 contacts and restore the rest' }))

    // A 409 will not come right by pressing again, so the dialog gets out of
    // the way and the page explains what to do instead.
    expect(await screen.findByText('This import cannot be undone as it stands.')).toBeVisible()
    expect(screen.getByText(detail)).toBeVisible()
    expect(
      screen.getByText(/Nothing was changed. Undo the merge on the contacts named above/),
    ).toBeVisible()
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('names the later import to undo first, and offers no way around it', async () => {
    const detail =
      'import run(s) 9 wrote over fields this run also wrote, on contact(s) 31; roll back the ' +
      'later run(s) first, newest first.'
    mockFetch(
      detailBackend(COMMITTED_RUN, [], () =>
        jsonResponse({ detail, code: 'superseded', run_ids: [9], contact_ids: [31] }, 409),
      ),
    )
    await renderApp('/imports/runs/7')

    fireEvent.click(await screen.findByRole('button', { name: 'Roll back this import' }))
    const dialog = within(await screen.findByRole('alertdialog'))
    fireEvent.click(dialog.getByRole('button', { name: 'Delete 2 contacts and restore the rest' }))

    expect(await screen.findByText('A later import has to be undone first.')).toBeVisible()
    expect(screen.getByText(detail)).toBeVisible()
    expect(screen.queryByText(/Undo the merge/)).toBeNull()
    expect(screen.queryByRole('button', { name: /anyway/ })).toBeNull()
  })

  it('says what a created contact gained, and rolls back anyway only when asked', async () => {
    const detail =
      '2 contact(s) this run created (31, 32) have gained things since the import that ' +
      'rolling back would delete with them: 3 interactions, 1 tag added by hand.'
    const calls: Call[] = []
    mockFetch(
      detailBackend(COMMITTED_RUN, calls, (call) =>
        call.query.get('force') === 'true'
          ? undefined
          : jsonResponse({ detail, code: 'created_contacts_changed', contact_ids: [31, 32] }, 409),
      ),
    )
    await renderApp('/imports/runs/7')

    fireEvent.click(await screen.findByRole('button', { name: 'Roll back this import' }))
    const dialog = within(await screen.findByRole('alertdialog'))
    fireEvent.click(dialog.getByRole('button', { name: 'Delete 2 contacts and restore the rest' }))

    expect(
      await screen.findByText('Rolling back would delete more than this import added.'),
    ).toBeVisible()
    expect(screen.getByText(detail)).toBeVisible()
    const rollbacks = () => calls.filter((call) => call.path === '/api/v1/imports/7/rollback')
    expect(rollbacks()).toHaveLength(1)
    expect(rollbacks()[0]?.query.get('force')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Delete them anyway and roll back' }))

    expect(await screen.findByText('Rolled back')).toBeVisible()
    expect(rollbacks()).toHaveLength(2)
    expect(rollbacks()[1]?.query.get('force')).toBe('true')
  })

  it('shows an archive import with every file\'s counts, and offers to roll it back', async () => {
    const report = {
      observed_at: ARCHIVE_RESULT.observed_at,
      owner_public_id: ARCHIVE_RESULT.owner_public_id,
      owner_by: ARCHIVE_RESULT.owner_by,
      connections: ARCHIVE_RESULT.connections,
      messages: ARCHIVE_RESULT.messages,
      invitations: ARCHIVE_RESULT.invitations,
      ignored_files: ARCHIVE_RESULT.ignored_files,
    }
    const archiveRun = {
      ...COMMITTED_RUN,
      id: 12,
      source_kind: 'archive' as const,
      filename: 'export.zip',
      preset: null,
      archive: report,
    }
    mockFetch(detailBackend(archiveRun))
    await renderApp('/imports/runs/12')

    expect(await screen.findByText(/from a LinkedIn data archive/)).toBeVisible()
    expect(screen.getByText('messages.csv')).toBeVisible()
    expect(screen.getByText('Invitations.csv')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Roll back this import' })).toBeVisible()
  })

  it('warns beforehand that a merge can make a rollback impossible', async () => {
    mockFetch(detailBackend())
    await renderApp('/imports/runs/7')

    expect(
      await screen.findByText(/the rollback is refused whole rather than half done/),
    ).toBeVisible()
    expect(
      screen.getByText(
        /with the record of where those values came from, so your own edits stay yours/,
      ),
    ).toBeVisible()
  })

  it('offers no rollback for a draft, and a way to finish it instead', async () => {
    mockFetch(detailBackend(DRAFT_RUN))
    await renderApp('/imports/runs/7')

    expect(await screen.findByText('Never committed')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Roll back this import' })).toBeNull()
    expect(screen.getByRole('link', { name: 'Finish this import' })).toHaveAttribute(
      'href',
      '/imports?run=7',
    )
  })

  it('says a run that was already undone cannot be undone again', async () => {
    mockFetch(detailBackend(ROLLED_BACK_RUN))
    await renderApp('/imports/runs/9')

    expect(await screen.findByText(/a run can only be rolled back once/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Roll back this import' })).toBeNull()
  })
})
