import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import {
  ARCHIVE_INSPECTION,
  CANDIDATE_ROW,
  COMMITTED_RUN,
  DRAFT_RUN,
  NINE_COLUMN_INSPECTION,
  PRESETS,
  PREVIEW_ROWS,
  UNRECOGNIZED_INSPECTION,
  type Call,
  backend,
  commitLike,
  csvFile,
} from './test-support'

/** `name\nÉlodie` in Windows-1252: the export shape issue #79 is about. */
function windows1252File(): File {
  const bytes = new Uint8Array([
    0x46, 0x69, 0x72, 0x73, 0x74, 0x20, 0x4e, 0x61, 0x6d, 0x65, 0x0a, 0xc9, 0x6c, 0x6f, 0x64, 0x69,
    0x65, 0x0a,
  ])
  return new File([bytes], 'windows-export.csv', { type: 'text/csv' })
}

/** The whole backend the wizard talks to, with every answer a happy path needs. */
function happyBackend(calls: Call[] = []) {
  let inspected = 0
  return backend(
    {
      'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
      'POST /api/v1/imports/inspect': () => {
        inspected += 1
        return jsonResponse(inspected === 1 ? ARCHIVE_INSPECTION : NINE_COLUMN_INSPECTION)
      },
      'POST /api/v1/imports': () => jsonResponse(DRAFT_RUN, 201),
      'POST /api/v1/imports/7/preview': () => jsonResponse(PREVIEW_ROWS),
      'GET /api/v1/imports/7/rows': () => jsonResponse({ items: [CANDIDATE_ROW], total: 1 }),
      // Refuses like the real one: 409 while row 3 has no decision.
      'POST /api/v1/imports/7/commit': commitLike([CANDIDATE_ROW.row_number], COMMITTED_RUN),
    },
    calls,
  )
}

async function chooseFile() {
  const input = screen.getByLabelText('CSV file')
  fireEvent.change(input, { target: { files: [csvFile()] } })
  return screen.findByText('Map the columns')
}

async function reachPreview() {
  await chooseFile()
  fireEvent.click(screen.getByRole('button', { name: 'Preview the import' }))
  await screen.findByText('What this import would do')
  // The resolved rows arrive after the run; the step's Continue waits for them.
  return screen.findByText('The first 4 rows')
}

async function reachCandidates() {
  await reachPreview()
  fireEvent.click(screen.getByRole('button', { name: 'Review 1 candidate' }))
  return screen.findByText('Decide the candidates')
}

describe('import wizard', () => {
  it('starts on the upload step and names the limits of what it accepts', async () => {
    mockFetch(happyBackend())
    await renderApp('/imports')

    expect(await screen.findByText('Choose a CSV')).toBeInTheDocument()
    expect(screen.getByText(/up to 8 MB of it/)).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Import steps' })).toBeInTheDocument()
  })

  it('reads a file and shows the detected preset and the columns it does not import', async () => {
    mockFetch(happyBackend())
    await renderApp('/imports')
    await chooseFile()

    expect(screen.getByText('The linkedin-archive preset fits this header best.')).toBeVisible()
    expect(screen.getByText(/3 lines above the header were skipped/)).toBeVisible()

    // A header nothing maps is called out, not dropped in silence.
    expect(screen.getByText(/1 column is not imported: “Notes”/)).toBeVisible()
    expect(screen.getByLabelText('Field for column Notes')).toHaveValue('')
    expect(screen.getByLabelText('Field for column First Name')).toHaveValue('first_name')
    expect(screen.getByText('6 of 7 columns mapped')).toBeVisible()
  })

  it('lets the detected preset be overridden', async () => {
    const calls: Call[] = []
    mockFetch(happyBackend(calls))
    await renderApp('/imports')
    await chooseFile()

    fireEvent.change(screen.getByLabelText('Preset'), { target: { value: 'nine-column' } })

    expect(await screen.findByText(/4 columns are not imported/)).toBeVisible()
    expect(screen.getByText('You are using the nine-column preset instead.')).toBeVisible()
    expect(screen.getByLabelText('Field for column Company')).toHaveValue('')
    expect(screen.getByText('3 of 7 columns mapped')).toBeVisible()

    const inspects = calls.filter((call) => call.path === '/api/v1/imports/inspect')
    expect(inspects).toHaveLength(2)
    expect(inspects[1]?.body).toMatchObject({ preset: 'nine-column' })
  })

  it('sends the mapping on screen, with the unmapped columns blanked out', async () => {
    const calls: Call[] = []
    mockFetch(happyBackend(calls))
    await renderApp('/imports')
    await chooseFile()

    fireEvent.change(screen.getByLabelText('Field for column Notes'), {
      target: { value: 'headline' },
    })
    fireEvent.change(screen.getByLabelText('Field for column Position'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview the import' }))
    await screen.findByText('What this import would do')

    const created = calls.find((call) => call.method === 'POST' && call.path === '/api/v1/imports')
    expect(created?.body).toMatchObject({
      filename: 'connections.csv',
      // Editing by hand makes the mapping the person's own, not the preset's.
      preset: null,
      mapping: { Notes: 'headline', Position: '', 'First Name': 'first_name' },
    })
  })

  it('shows matches, new contacts, candidates, skips, and the fields provenance refuses', async () => {
    mockFetch(happyBackend())
    await renderApp('/imports')
    await reachPreview()

    const main = within(screen.getByRole('main'))
    expect(main.getByText('Matches a contact')).toBeVisible()
    expect(main.getByText('New contact')).toBeVisible()
    expect(main.getByText('Needs a decision')).toBeVisible()
    expect(main.getByText('Skipped')).toBeVisible()

    expect(main.getByText('Rosalind Quillfeather')).toBeVisible()
    expect(main.getByText('contact #11 by email')).toBeVisible()
    expect(main.getByText('could be #12 or #13')).toBeVisible()
    expect(main.getByText('no column identifies anybody')).toBeVisible()

    // The refused field is the thing a person cannot discover after committing.
    expect(main.getByText('1 value in these rows will not be written.')).toBeVisible()
    expect(main.getByText(/Affected fields: Company/)).toBeVisible()
    // The field name is its own element, so match on the whole line.
    const refusedLine = main.getByText(
      (_text, element) =>
        element?.tagName === 'LI' &&
        (element.textContent ?? '').includes(
          'Company refused: the edit you made “Nimbus Kettle Co” stays, and ' +
            '“Nimbus Kettle Works” from the file is not written.',
        ),
    )
    expect(refusedLine).toBeVisible()
  })

  it('names the cells the importer dropped on rows that still land', async () => {
    mockFetch(happyBackend())
    await renderApp('/imports')
    await reachPreview()

    const main = within(screen.getByRole('main'))
    expect(main.getByText('1 row has a cell the importer cannot use.')).toBeVisible()
    expect(
      main.getByText(
        (_text, element) =>
          element?.tagName === 'P' &&
          element.textContent ===
            "Dropped: Provider Id: 'ab12cd' is not a LinkedIn URN (urn:li:...)",
      ),
    ).toBeVisible()
  })

  it('keeps commit out of reach while a candidate is undecided', async () => {
    mockFetch(happyBackend())
    await renderApp('/imports')
    await reachCandidates()

    expect(screen.getByText('0 of 1 decided, 1 to go.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Go to commit' }))
    await screen.findByText('Commit the import')

    expect(screen.getByRole('button', { name: 'Commit 4 rows' })).toBeDisabled()
    expect(screen.getByText(/Decide every candidate first/)).toBeVisible()
  })

  it('commits once every candidate has a decision', async () => {
    const calls: Call[] = []
    mockFetch(happyBackend(calls))
    await renderApp('/imports')
    await reachCandidates()

    expect(screen.getByText('Row 3: Imogen Pallisade')).toBeVisible()
    expect(screen.getByText(/today: Job title “Escapement Fitter”/)).toBeVisible()
    // The stored candidates loaded cleanly: no 422 from a limit or a resolution
    // the route does not take.
    expect(screen.queryByRole('alert')).toBeNull()
    const rows = calls.find((call) => call.path === '/api/v1/imports/7/rows')
    expect(rows?.query.get('resolution')).toBe('candidate')
    expect(Number(rows?.query.get('limit'))).toBeLessThanOrEqual(500)

    fireEvent.click(screen.getByRole('radio', { name: /Merge into contact #12/ }))
    expect(screen.getByText('1 of 1 decided.')).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Go to commit' }))
    await screen.findByText('Commit the import')

    const commitButton = screen.getByRole('button', { name: 'Commit 4 rows' })
    expect(commitButton).toBeEnabled()
    fireEvent.click(commitButton)

    expect(await screen.findByText('Imported connections.csv')).toBeVisible()
    expect(screen.getByRole('link', { name: 'See your contacts' })).toHaveAttribute(
      'href',
      '/contacts',
    )
    expect(screen.getByRole('link', { name: 'Open this run' })).toHaveAttribute(
      'href',
      '/imports/runs/7',
    )

    const committed = calls.find((call) => call.path === '/api/v1/imports/7/commit')
    expect(committed?.body).toEqual({
      decisions: [{ row_number: 3, kind: 'merge_into', contact_id: 12 }],
      skip_undecided: false,
    })
  })

  it('lets undecided candidates be skipped on purpose', async () => {
    const calls: Call[] = []
    mockFetch(happyBackend(calls))
    await renderApp('/imports')
    await reachCandidates()

    fireEvent.click(screen.getByRole('button', { name: 'Go to commit' }))
    await screen.findByText('Commit the import')

    fireEvent.click(screen.getByRole('checkbox', { name: 'Skip the 1 undecided candidate' }))
    fireEvent.click(screen.getByRole('button', { name: 'Commit 4 rows' }))

    expect(await screen.findByText('Imported connections.csv')).toBeVisible()
    const committed = calls.find((call) => call.path === '/api/v1/imports/7/commit')
    expect(committed?.body).toEqual({ decisions: [], skip_undecided: true })
  })

  it('decides the remaining candidates as new contacts in one go', async () => {
    mockFetch(happyBackend())
    await renderApp('/imports')
    await reachCandidates()

    fireEvent.click(screen.getByRole('button', { name: 'Create a new contact for the rest' }))
    expect(screen.getByText('1 of 1 decided.')).toBeVisible()
    expect(screen.getByRole('radio', { name: 'Create a new contact' })).toBeChecked()
  })

  it('saves the mapping on screen as a named preset', async () => {
    const calls: Call[] = []
    mockFetch(
      backend(
        {
          'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
          'POST /api/v1/imports/inspect': () => jsonResponse(ARCHIVE_INSPECTION),
          'PUT /api/v1/imports/presets/my-export': () =>
            jsonResponse({ name: 'my-export', builtin: false, mapping: {} }),
        },
        calls,
      ),
    )
    await renderApp('/imports')
    await chooseFile()

    fireEvent.change(screen.getByLabelText('Preset name'), { target: { value: 'my-export' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save preset' }))

    expect(await screen.findByText('Saved as my-export.')).toBeVisible()
    const saved = calls.find((call) => call.method === 'PUT')
    expect(saved?.body).toMatchObject({ mapping: { 'First Name': 'first_name' } })
    // The blank column never reaches a preset.
    expect((saved?.body as { mapping: Record<string, string> }).mapping).not.toHaveProperty('Notes')
  })
})

describe('a CSV no preset recognizes', () => {
  it('reaches the mapping screen instead of dead-ending on upload', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        // The API used to answer 422 here, which left the only screen that can
        // fix the file out of reach. It now returns the columns.
        'POST /api/v1/imports/inspect': () => jsonResponse(UNRECOGNIZED_INSPECTION),
      }),
    )
    await renderApp('/imports')
    await screen.findByText('Choose a CSV')

    fireEvent.change(screen.getByLabelText('CSV file'), {
      target: {
        files: [csvFile('Given,Surname,Mail\nHortensia,Blennerhassett,h@tarnish.example\n')],
      },
    })

    expect(await screen.findByText('Map the columns')).toBeVisible()
    expect(
      screen.getByText(
        'No built-in preset matches these columns, so start from “Map the columns by hand”.',
      ),
    ).toBeVisible()
    expect(screen.getByText('0 of 3 columns mapped')).toBeVisible()
    expect(screen.getByText(/3 columns are not imported/)).toBeVisible()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('will not read a file into a run until a column is mapped', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/inspect': () => jsonResponse(UNRECOGNIZED_INSPECTION),
      }),
    )
    await renderApp('/imports')
    await screen.findByText('Choose a CSV')
    fireEvent.change(screen.getByLabelText('CSV file'), {
      target: {
        files: [csvFile('Given,Surname,Mail\nHortensia,Blennerhassett,h@tarnish.example\n')],
      },
    })
    await screen.findByText('Map the columns')

    expect(screen.getByRole('button', { name: 'Preview the import' })).toBeDisabled()
    expect(screen.getByText('Map at least one column first.')).toBeVisible()
  })
})

describe('file encoding (issue #79)', () => {
  it('reads a Windows export as Windows-1252 and says so', async () => {
    const calls: Call[] = []
    mockFetch(happyBackend(calls))
    await renderApp('/imports')
    await screen.findByText('Choose a CSV')

    fireEvent.change(screen.getByLabelText('CSV file'), {
      target: { files: [windows1252File()] },
    })
    await screen.findByText('Map the columns')

    expect(screen.getByLabelText('Read as')).toHaveValue('windows-1252')
    expect(
      screen.getByText(
        'the bytes are not valid UTF-8, so the usual Windows export encoding was assumed',
      ),
    ).toBeVisible()
    expect(screen.getByText(/Check an accented name in the table below/)).toBeVisible()

    // The decoded text, not U+FFFD soup, is what reaches the API.
    const inspect = calls.find((call) => call.path === '/api/v1/imports/inspect')
    expect((inspect?.body as { content: string }).content).toBe('First Name\nÉlodie\n')
  })

  it('says UTF-8 was read as UTF-8, with no warning', async () => {
    mockFetch(happyBackend())
    await renderApp('/imports')
    await chooseFile()

    expect(screen.getByLabelText('Read as')).toHaveValue('utf-8')
    expect(screen.getByText('the bytes are valid UTF-8')).toBeVisible()
    expect(screen.queryByText(/Check an accented name/)).toBeNull()
  })

  it('says so when an encoding override fails instead of snapping back in silence', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/inspect': (call) => {
          const { content } = call.body as { content: string }
          // Read as UTF-16 the bytes come back as something else entirely, so
          // the header line is no longer there to find.
          return content.includes('First Name')
            ? jsonResponse(ARCHIVE_INSPECTION)
            : jsonResponse({ detail: 'the file has no header row' }, 422)
        },
      }),
    )
    await renderApp('/imports')
    await chooseFile()

    fireEvent.change(screen.getByLabelText('Read as'), { target: { value: 'utf-16be' } })

    expect(await screen.findByRole('alert')).toHaveTextContent('the file has no header row')
  })

  it('lets the encoding be overridden, and reads the file again with it', async () => {
    const calls: Call[] = []
    mockFetch(happyBackend(calls))
    await renderApp('/imports')
    await chooseFile()

    fireEvent.change(screen.getByLabelText('Read as'), { target: { value: 'windows-1252' } })
    await screen.findByText('you chose it')

    expect(screen.getByLabelText('Read as')).toHaveValue('windows-1252')
    expect(calls.filter((call) => call.path === '/api/v1/imports/inspect')).toHaveLength(2)
  })
})

describe('a draft whose meaning has changed since it was read', () => {
  /**
   * The draft recorded no candidates; the preview, re-resolved against the
   * database as it is now, finds one. `commit` re-resolves too, so gating on
   * the stored count enables a button that answers 409 with nothing left to try.
   */
  function staleBackend(calls: Call[] = []) {
    const staleRun = { ...DRAFT_RUN, candidate_count: 0, matched_count: 2, created_count: 1 }
    return backend(
      {
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'GET /api/v1/imports/7': () => jsonResponse(staleRun),
        // Live: row 3 needs a decision, whatever the draft says.
        'POST /api/v1/imports/7/preview': () => jsonResponse(PREVIEW_ROWS),
        // Stored: the draft recorded none, so this page is empty.
        'GET /api/v1/imports/7/rows': () => jsonResponse({ items: [], total: 0 }),
        'POST /api/v1/imports/7/commit': commitLike([3], COMMITTED_RUN),
      },
      calls,
    )
  }

  it('says the draft has gone stale and still routes through the candidates', async () => {
    mockFetch(staleBackend())
    await renderApp('/imports?run=7')

    expect(await screen.findByText('The first 4 rows')).toBeVisible()
    expect(screen.getByText('This draft was read before your contacts changed.')).toBeVisible()
    expect(screen.getByText(/1 row needs a decision that the draft did not record/)).toBeVisible()

    // Not "Go to commit": the live resolutions decide the label.
    fireEvent.click(screen.getByRole('button', { name: 'Review 1 candidate' }))
    expect(await screen.findByText('Decide the candidates')).toBeVisible()
    expect(screen.getByText(/Your contacts have changed since this file was read/)).toBeVisible()
    expect(screen.getByText('Row 3: Imogen Pallisade')).toBeVisible()
    expect(screen.getByText('0 of 1 decided, 1 to go.')).toBeVisible()
  })

  it("blocks the commit on the live count, not the draft's", async () => {
    mockFetch(staleBackend())
    await renderApp('/imports?run=7')
    await screen.findByText('The first 4 rows')
    fireEvent.click(screen.getByRole('button', { name: 'Review 1 candidate' }))
    await screen.findByText('Decide the candidates')
    fireEvent.click(screen.getByRole('button', { name: 'Go to commit' }))
    await screen.findByText('Commit the import')

    expect(screen.getByRole('button', { name: 'Commit 4 rows' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Back to the candidates' }))
    expect(await screen.findByText('Decide the candidates')).toBeVisible()
  })

  it('offers a way out when the API refuses a commit this screen thought was ready', async () => {
    // The worst case: nothing on screen knows row 3 is a candidate, so the
    // button is enabled and the API refuses. Skipping has to be reachable.
    const calls: Call[] = []
    mockFetch(
      backend(
        {
          'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
          'GET /api/v1/imports/7': () =>
            jsonResponse({ ...DRAFT_RUN, candidate_count: 0, matched_count: 3 }),
          'POST /api/v1/imports/7/preview': () =>
            jsonResponse(PREVIEW_ROWS.filter((row) => row.resolution !== 'candidate')),
          'GET /api/v1/imports/7/rows': () => jsonResponse({ items: [], total: 0 }),
          'POST /api/v1/imports/7/commit': commitLike([3], COMMITTED_RUN),
        },
        calls,
      ),
    )
    await renderApp('/imports?run=7')
    await screen.findByText('The first 3 rows')

    fireEvent.click(screen.getByRole('button', { name: 'Go to commit' }))
    await screen.findByText('Commit the import')

    const button = screen.getByRole('button', { name: 'Commit 4 rows' })
    expect(button).toBeEnabled()
    fireEvent.click(button)

    expect(
      await screen.findByText('The import was refused: a row resolves to a candidate right now.'),
    ).toBeVisible()
    expect(screen.getByRole('alert')).toHaveTextContent('rows 3')
    // Blocked now, but with the escape on screen rather than a dead end.
    expect(screen.getByRole('button', { name: 'Commit 4 rows' })).toBeDisabled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Skip any candidate nobody has decided' }))
    fireEvent.click(screen.getByRole('button', { name: 'Commit 4 rows' }))

    expect(await screen.findByText('Imported connections.csv')).toBeVisible()
    const last = calls.filter((call) => call.path === '/api/v1/imports/7/commit').at(-1)
    expect(last?.body).toEqual({ decisions: [], skip_undecided: true })
  })
})

describe('finishing a draft from the history', () => {
  it('picks a draft up at the preview without asking for the file again', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'GET /api/v1/imports/7': () => jsonResponse(DRAFT_RUN),
        'POST /api/v1/imports/7/preview': () => jsonResponse(PREVIEW_ROWS),
        'GET /api/v1/imports/7/rows': () => jsonResponse({ items: [CANDIDATE_ROW], total: 1 }),
      }),
    )
    await renderApp('/imports?run=7')

    expect(await screen.findByText('The first 4 rows')).toBeVisible()
    // There is no file in hand, so the mapping cannot be changed from here.
    expect(screen.queryByRole('button', { name: 'Change the mapping' })).toBeNull()
  })

  it('keeps the commit result when the run comes back committed underneath it', async () => {
    // Committing invalidates the run query, and the real API then answers
    // `committed`. Judging the status above the result replaced it with
    // "already committed" — the same way the rollback summary was once lost.
    let applied = false
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'GET /api/v1/imports/7': () => jsonResponse(applied ? COMMITTED_RUN : DRAFT_RUN),
        'POST /api/v1/imports/7/preview': () => jsonResponse(PREVIEW_ROWS),
        'GET /api/v1/imports/7/rows': () => jsonResponse({ items: [CANDIDATE_ROW], total: 1 }),
        'POST /api/v1/imports/7/commit': (call) => {
          const answer = commitLike([CANDIDATE_ROW.row_number], COMMITTED_RUN)(call)
          if (answer.status === 200) applied = true
          return answer
        },
      }),
    )
    await renderApp('/imports?run=7')
    await screen.findByText('The first 4 rows')

    fireEvent.click(screen.getByRole('button', { name: 'Review 1 candidate' }))
    await screen.findByText('Decide the candidates')
    fireEvent.click(screen.getByRole('radio', { name: /Merge into contact #12/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Go to commit' }))
    await screen.findByText('Commit the import')
    fireEvent.click(screen.getByRole('button', { name: 'Commit 4 rows' }))

    expect(await screen.findByText('Imported connections.csv')).toBeVisible()
    await waitFor(() => {
      expect(screen.queryByText(/This import is already/)).toBeNull()
    })
    expect(screen.getByText('Imported connections.csv')).toBeVisible()
  })

  it('refuses to reopen a run that was already committed', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'GET /api/v1/imports/7': () => jsonResponse(COMMITTED_RUN),
      }),
    )
    await renderApp('/imports?run=7')

    expect(await screen.findByText('This import is already committed')).toBeVisible()
  })
})

describe('import wizard failures', () => {
  it('refuses an empty file without calling the API', async () => {
    const calls: Call[] = []
    mockFetch(happyBackend(calls))
    await renderApp('/imports')
    await screen.findByText('Choose a CSV')

    fireEvent.change(screen.getByLabelText('CSV file'), {
      target: { files: [new File([], 'nothing.csv')] },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('nothing.csv is empty.')
    expect(calls.some((call) => call.path === '/api/v1/imports/inspect')).toBe(false)
  })

  it("shows the backend's own reason when a file cannot be read", async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/inspect': () =>
          // A quoted field past the CSV reader's 128 KiB limit: a 422 since the
          // review of P1-04, not a 500.
          jsonResponse(
            { detail: 'row 3: a quoted field runs past 131072 characters without closing' },
            422,
          ),
      }),
    )
    await renderApp('/imports')
    await screen.findByText('Choose a CSV')

    fireEvent.change(screen.getByLabelText('CSV file'), {
      target: { files: [csvFile('Notes:\nnothing below this line\n')] },
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'a quoted field runs past 131072 characters without closing',
    )
  })

  it('offers a retry when the preview cannot be resolved', async () => {
    let attempts = 0
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/inspect': () => jsonResponse(ARCHIVE_INSPECTION),
        'POST /api/v1/imports': () => jsonResponse(DRAFT_RUN, 201),
        'POST /api/v1/imports/7/preview': () => {
          attempts += 1
          return attempts === 1
            ? jsonResponse({ detail: 'database is locked' }, 409)
            : jsonResponse(PREVIEW_ROWS)
        },
      }),
    )
    await renderApp('/imports')
    await chooseFile()
    fireEvent.click(screen.getByRole('button', { name: 'Preview the import' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('database is locked')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Matches a contact')).toBeVisible()
  })
})
