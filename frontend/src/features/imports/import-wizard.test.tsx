import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import {
  ARCHIVE_INSPECTION,
  ARCHIVE_RESULT,
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
  zipFile,
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
  const input = screen.getByLabelText('File to import')
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

    expect(await screen.findByText('Choose a file to import')).toBeInTheDocument()
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
    await screen.findByText('Choose a file to import')

    fireEvent.change(screen.getByLabelText('File to import'), {
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
    await screen.findByText('Choose a file to import')
    fireEvent.change(screen.getByLabelText('File to import'), {
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
    await screen.findByText('Choose a file to import')

    fireEvent.change(screen.getByLabelText('File to import'), {
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

describe('the commit step with no candidates (#94)', () => {
  it('goes back to the preview, and says so', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'GET /api/v1/imports/7': () =>
          jsonResponse({ ...DRAFT_RUN, candidate_count: 0, matched_count: 3 }),
        'POST /api/v1/imports/7/preview': () =>
          jsonResponse(PREVIEW_ROWS.filter((row) => row.resolution !== 'candidate')),
        'GET /api/v1/imports/7/rows': () => jsonResponse({ items: [], total: 0 }),
      }),
    )
    await renderApp('/imports?run=7')
    await screen.findByText('The first 3 rows')
    fireEvent.click(screen.getByRole('button', { name: 'Go to commit' }))
    await screen.findByText('Commit the import')

    expect(screen.queryByRole('button', { name: 'Back to the candidates' })).toBeNull()
    expect(screen.queryByText(/Of \d+ candidates?,/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Back to the preview' }))

    expect(await screen.findByText('The first 3 rows')).toBeVisible()
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
    await screen.findByText('Choose a file to import')

    fireEvent.change(screen.getByLabelText('File to import'), {
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
    await screen.findByText('Choose a file to import')

    fireEvent.change(screen.getByLabelText('File to import'), {
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

describe('the archive shape: a zip, or a lone message/invitation file (P1-21)', () => {
  async function chooseArchive(file: File) {
    await screen.findByText('Choose a file to import')
    fireEvent.change(screen.getByLabelText('File to import'), { target: { files: [file] } })
  }

  it('recognizes a zip and says so before anything is sent', async () => {
    const calls: Call[] = []
    mockFetch(backend({ 'GET /api/v1/imports/presets': () => jsonResponse(PRESETS) }, calls))
    await renderApp('/imports')
    await chooseArchive(zipFile('my-export.zip'))

    expect(await screen.findByText('Recognized: a LinkedIn data archive')).toBeVisible()
    expect(screen.getByText('my-export.zip')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Import' })).toBeEnabled()
    // Recognizing it is not importing it.
    expect(calls.some((call) => call.path === '/api/v1/imports/archive')).toBe(false)
  })

  it('backs out to the upload step without importing anything', async () => {
    const calls: Call[] = []
    mockFetch(backend({ 'GET /api/v1/imports/presets': () => jsonResponse(PRESETS) }, calls))
    await renderApp('/imports')
    await chooseArchive(zipFile())
    await screen.findByText('Recognized: a LinkedIn data archive')

    fireEvent.click(screen.getByRole('button', { name: 'Choose a different file' }))

    expect(await screen.findByText('Choose a file to import')).toBeVisible()
    expect(calls.some((call) => call.path === '/api/v1/imports/archive')).toBe(false)
  })

  it('recovers cleanly when a zip is dropped while an abandoned CSV is still being read', async () => {
    // The race review finding 5 caught: `open.mutate` for the CSV is still in
    // flight (nothing here awaits it) when the zip is chosen, which switches
    // straight to the archive screen — recognizing a file by its name alone
    // is synchronous. The CSV's `inspect` call resolves on its own later; if
    // that stale result still lands, backing out of the archive screen would
    // show the mapping screen for a file nobody chose this time.
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/inspect': () => jsonResponse(ARCHIVE_INSPECTION),
      }),
    )
    await renderApp('/imports')
    const input = await screen.findByLabelText('File to import')
    fireEvent.change(input, { target: { files: [csvFile(undefined, 'Connections.csv')] } })
    fireEvent.change(input, { target: { files: [zipFile('export.zip')] } })

    expect(await screen.findByText('Recognized: a LinkedIn data archive')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Choose a different file' }))

    expect(await screen.findByText('Choose a file to import')).toBeVisible()
    expect(screen.queryByText('Map the columns')).toBeNull()
  })

  it("shows its own step nav, not the CSV pipeline's", async () => {
    mockFetch(backend({ 'GET /api/v1/imports/presets': () => jsonResponse(PRESETS) }))
    await renderApp('/imports')
    await chooseArchive(zipFile())
    await screen.findByText('Recognized: a LinkedIn data archive')

    const nav = within(screen.getByRole('navigation', { name: 'Import steps' }))
    expect(nav.getByText('Review')).toBeVisible()
    expect(nav.queryByText('Map columns')).toBeNull()
    expect(nav.queryByText('Preview')).toBeNull()
    expect(nav.queryByText('Candidates')).toBeNull()
    expect(nav.queryByText('Commit')).toBeNull()
  })

  it('imports the zip on request and reports what each file contributed', async () => {
    const calls: Call[] = []
    mockFetch(
      backend(
        {
          'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
          'POST /api/v1/imports/archive': () => jsonResponse(ARCHIVE_RESULT, 201),
        },
        calls,
      ),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile('export.zip'))
    await screen.findByText('Recognized: a LinkedIn data archive')

    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    expect(await screen.findByText('Imported export.zip')).toBeVisible()
    expect(
      screen.getByText(
        '7 new contacts, 1 contact updated, 1 needs a closer look, 8 message interactions, ' +
          '2 invitations.',
      ),
    ).toBeVisible()
    // Each file's own numbers, not one opaque total.
    expect(screen.getByText('Connections.csv')).toBeVisible()
    expect(screen.getByText('messages.csv')).toBeVisible()
    expect(screen.getByText('Invitations.csv')).toBeVisible()
    expect(screen.getByText(/1 more looked like someone you might already have/)).toBeVisible()
    // Recorded as a run (#132): in the history, and undone from there.
    expect(screen.getByRole('link', { name: 'Open this import' })).toHaveAttribute(
      'href',
      '/imports/runs/12',
    )

    const upload = calls.find((call) => call.path === '/api/v1/imports/archive')
    expect(upload?.method).toBe('POST')
    // Not `toBeInstanceOf(File)`: the request that carried it was built by
    // Node's real `Request`, which parses its own `multipart/form-data` body
    // back into its own `File` class — a different one than the jsdom global
    // this test file sees, even though it is the same file in every way that
    // matters to the app. `.name` is what the endpoint actually reads.
    const uploaded = (upload?.body as FormData).get('file') as ({ name?: unknown } & Blob) | null
    expect(uploaded).not.toBeNull()
    expect(uploaded?.name).toBe('export.zip')
    // The bytes themselves, not only the field name and filename: the whole
    // point of `registerFileContent`/`fileContents` in `@/test/fetch` is that
    // an upload's content actually reaches the request, so assert on it here
    // rather than leaving that machinery unexercised (review finding 11).
    await expect(uploaded?.text()).resolves.toBe('not a real zip; the frontend never reads it')

    fireEvent.click(screen.getByRole('button', { name: 'Import another file' }))
    expect(await screen.findByText('Choose a file to import')).toBeVisible()
  })

  it('never says nothing happened when the whole point was that nothing matched (messages only)', async () => {
    // Case A from review finding 1: a lone `messages.csv` into a database
    // that doesn't have those contacts yet — the exact path the "already
    // unzipped it by hand" panel recommends. 4,211 rows, 0 attributed.
    const noMatch = {
      ...ARCHIVE_RESULT,
      filename: 'messages.csv',
      connections: {
        ...ARCHIVE_RESULT.connections,
        rows: 0,
        created: 0,
        updated: 0,
        needs_review: 0,
      },
      messages: {
        rows: 4211,
        conversations: 812,
        attributed: 0,
        no_counterpart: 20,
        group_threads: 24,
        unknown_contact: 768,
        no_owner: 0,
        added: 0,
        already_present: 0,
        undated: 3,
        outbound: 0,
        inbound: 0,
      },
      invitations: { ...ARCHIVE_RESULT.invitations, rows: 0, added: 0 },
    }
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () => jsonResponse(noMatch, 201),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(csvFile('CONVERSATION ID,FROM,TO,DATE,CONTENT\n', 'messages.csv'))
    await screen.findByText('Recognized: your LinkedIn message history, on its own')
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    await screen.findByText('Imported messages.csv')
    expect(screen.queryByText(/Nothing new/)).toBeNull()
    expect(
      screen.getByText(/Read 4211 message rows, but none of them matched a contact already here/),
    ).toBeVisible()
    expect(screen.getByText(/import your connections first/)).toBeVisible()
  })

  it('never says nothing happened when every connection needs a decision instead', async () => {
    // Case B from review finding 1.
    const allNeedsReview = {
      ...ARCHIVE_RESULT,
      connections: {
        rows: 620,
        created: 0,
        updated: 0,
        needs_review: 620,
        skipped: 0,
        with_email: 0,
        undated: 0,
      },
      messages: { ...ARCHIVE_RESULT.messages, rows: 0, attributed: 0, added: 0 },
      invitations: { ...ARCHIVE_RESULT.invitations, rows: 0, added: 0 },
    }
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () => jsonResponse(allNeedsReview, 201),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile('export.zip'))
    await screen.findByText('Recognized: a LinkedIn data archive')
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    await screen.findByText(/^Imported/)
    expect(screen.queryByText(/Nothing new/)).toBeNull()
    expect(screen.getByText('620 need a closer look.')).toBeVisible()
    // The advice now says to unzip first, which is what actually gets
    // somebody from "I uploaded the zip" to "Connections.csv on its own"
    // (review finding 7 — the panel promises no unzipping for the zip path,
    // so the one place that stops being true has to say so).
    expect(screen.getByText(/unzip the archive/i)).toBeVisible()
  })

  it('names the files the archive carried but did not read, without overclaiming the count', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse({ ...ARCHIVE_RESULT, ignored_files: ['Positions.csv', 'Skills.csv'] }, 201),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile())
    await screen.findByText('Recognized: a LinkedIn data archive')
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))
    await screen.findByText(/^Imported/)

    // Not "the rest of the export" (review finding 8): `ignored_files` is
    // only the unrecognized `.csv` tables, not every file a real export has.
    expect(
      screen.getByText(/netkeeper also found 2 other tables in the export and didn.t read them\./),
    ).toBeVisible()
    expect(screen.getByText(/Positions\.csv, Skills\.csv/)).toBeVisible()
    expect(screen.queryByText(/the rest of the export/)).toBeNull()
  })

  it('names a file read as message history that is not messages.csv (issue #74)', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse(
            { ...ARCHIVE_RESULT, unfamiliar_message_files: ['interview_prep_messages.csv'] },
            201,
          ),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile())
    await screen.findByText('Recognized: a LinkedIn data archive')
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))
    await screen.findByText(/^Imported/)

    const note = screen.getByText(/Also read as message history, although it is not messages\.csv/)
    expect(note).toBeVisible()
    expect(note).toHaveTextContent('interview_prep_messages.csv')
    expect(note).toHaveTextContent(/assistant.s chat log/)
  })

  it('says nothing about unfamiliar message files when there are none', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () => jsonResponse(ARCHIVE_RESULT, 201),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile())
    await screen.findByText('Recognized: a LinkedIn data archive')
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))
    await screen.findByText(/^Imported/)
    expect(screen.queryByText(/Also read as message history/)).toBeNull()
  })

  it('recognizes a lone messages.csv and imports it with no mapping step', async () => {
    const calls: Call[] = []
    const messagesOnly = {
      ...ARCHIVE_RESULT,
      filename: 'messages.csv',
      connections: { ...ARCHIVE_RESULT.connections, rows: 0, created: 0, updated: 0 },
      invitations: { ...ARCHIVE_RESULT.invitations, rows: 0, added: 0 },
    }
    mockFetch(
      backend(
        {
          'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
          'POST /api/v1/imports/archive': () => jsonResponse(messagesOnly, 201),
        },
        calls,
      ),
    )
    await renderApp('/imports')
    await chooseArchive(csvFile('CONVERSATION ID,FROM,TO,DATE,CONTENT\n', 'messages.csv'))

    expect(
      await screen.findByText('Recognized: your LinkedIn message history, on its own'),
    ).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    expect(await screen.findByText('Imported messages.csv')).toBeVisible()
    // Only the file that was actually uploaded gets a card.
    expect(screen.queryByText('Connections.csv')).toBeNull()
    expect(screen.queryByText('Invitations.csv')).toBeNull()
    expect(screen.getByText('messages.csv')).toBeVisible()

    const upload = calls.find((call) => call.path === '/api/v1/imports/archive')
    const uploaded = (upload?.body as FormData).get('file') as { name?: unknown } | null
    expect(uploaded).not.toBeNull()
    expect(uploaded?.name).toBe('messages.csv')
  })

  it('recognizes a lone Invitations.csv the same way', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse(
            {
              ...ARCHIVE_RESULT,
              filename: 'Invitations.csv',
              connections: { ...ARCHIVE_RESULT.connections, rows: 0, created: 0, updated: 0 },
              messages: { ...ARCHIVE_RESULT.messages, rows: 0, added: 0 },
            },
            201,
          ),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(csvFile('Direction,From,To,Sent At\n', 'Invitations.csv'))

    expect(
      await screen.findByText('Recognized: your LinkedIn invitation history, on its own'),
    ).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))
    expect(await screen.findByText('Imported Invitations.csv')).toBeVisible()
  })

  it('still maps Connections.csv on its own, and says the zip carries more', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/inspect': () => jsonResponse(ARCHIVE_INSPECTION),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(csvFile(undefined, 'Connections.csv'))

    expect(await screen.findByText('Map the columns')).toBeVisible()
    expect(
      screen.getByText(/This looks like a LinkedIn Connections export on its own/),
    ).toBeVisible()
  })

  it('explains an export the endpoint does not recognize', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse(
            {
              detail:
                'random.zip: no Connections.csv, messages.csv, or Invitations.csv table in the archive',
              code: 'wrong_archive',
            },
            422,
          ),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile('random.zip'))
    await screen.findByText('Recognized: a LinkedIn data archive')

    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "This doesn't look like a LinkedIn export",
    )
    expect(screen.getByText(/random\.zip: no Connections\.csv/)).toBeVisible()
    // The way out is still on screen, and so is another try.
    expect(screen.getByRole('button', { name: 'Choose a different file' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Import' })).toBeEnabled()
  })

  it('explains a zip guard failure as a size or complexity problem, not jargon', async () => {
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse(
            {
              detail:
                'bomb.zip: member Skills.csv compresses 500x, over the 100x ratio a real export ' +
                'never approaches',
              code: 'compression_ratio_too_high',
            },
            422,
          ),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile('bomb.zip'))
    await screen.findByText('Recognized: a LinkedIn data archive')

    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This file is bigger or stranger than a real LinkedIn export',
    )
    expect(screen.getByText(/bomb\.zip: member Skills\.csv compresses 500x/)).toBeVisible()
  })

  it("falls back to a plain, non-contradictory explanation for a message it doesn't recognize", async () => {
    // Not one of the three current guesses, and no `code` — this is what a
    // message #124 reworded (or one this PR never anticipated) looks like.
    // The old default asserted "doesn't look like a zip or a CSV netkeeper
    // recognizes," which review finding 3 caught contradicting the backend's
    // own, more specific text in two real cases. Nothing here should ever
    // say what the file *is*.
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse({ detail: 'export.zip: unexpected end of central directory record' }, 422),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile('export.zip'))
    await screen.findByText('Recognized: a LinkedIn data archive')

    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("netkeeper couldn't import this file")
    expect(screen.getByText(/downloading a fresh copy from LinkedIn/)).toBeVisible()
    expect(
      screen.getByText(/export\.zip: unexpected end of central directory record/),
    ).toBeVisible()
  })

  it("keys guidance off a machine-readable code when the backend sends one, even if the message doesn't match anything guessed", async () => {
    // Built against a shape that has not landed (see archive-flow.tsx's own
    // comment): once the archive lane adds `code` to a 422, guidance no
    // longer depends on matching its prose at all.
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse(
            { detail: 'a message shaped nothing like any guess in this file', code: 'nested_zip' },
            422,
          ),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile('export.zip.zip'))
    await screen.findByText('Recognized: a LinkedIn data archive')

    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('This zip has another zip inside it')
    expect(screen.getByText(/upload the file inside it/)).toBeVisible()
  })

  it.each([
    ['encrypted', 'This zip is password-protected'],
    ['malformed_table', 'netkeeper found the table it wanted but could not read it'],
    ['too_large', 'This file is bigger or stranger than a real LinkedIn export'],
    ['too_many_members', 'This file is bigger or stranger than a real LinkedIn export'],
    ['unsafe_member_path', 'This zip has a file netkeeper will not open'],
    ['not_a_zip', "This doesn't look like a LinkedIn export"],
  ])('answers %s with its own guidance', async (code, headline) => {
    // The codes a person is least likely to hit and most likely to be
    // confused by. One test each, because the mapping is the whole feature:
    // the guidance is what tells them what to do, and the backend's message
    // for these says what happened, not what to try next.
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse({ detail: `export.zip: refused as ${code}`, code }, 422),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile('export.zip'))
    await screen.findByText('Recognized: a LinkedIn data archive')

    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(headline)
    expect(screen.getByText(new RegExp(`refused as ${code}`))).toBeVisible()
  })

  it('gives the honest default, not a wrong specific answer, when a refusal carries no code', async () => {
    // Every archive refusal carries a code (#124), so this is a failure from
    // somewhere else, or a backend older than the code it sends. The message
    // below reads exactly like a corrupt download; guessing from its words is
    // what this screen used to do, and what pointed people at the wrong next
    // step when the backend reworded one. Saying less is the fix.
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse({ detail: 'export.zip: corrupt central directory' }, 422),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile('export.zip'))
    await screen.findByText('Recognized: a LinkedIn data archive')

    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("netkeeper couldn't import this file")
    // The backend's own words are still on screen, which is where the
    // specific "what to do" lives when nothing here knows better.
    expect(screen.getByText(/export\.zip: corrupt central directory/)).toBeVisible()
    // And it never asserts what the file is.
    expect(alert).not.toHaveTextContent("didn't come through in one piece")
    expect(alert).not.toHaveTextContent("doesn't look like a zip or a CSV")
  })

  it('never tells someone to check they uploaded the right file when they already did (a corrupt download)', async () => {
    // #124 blocker 1: a corrupt/truncated download is "the likeliest failure
    // a real person will hit" per that PR's own review. Keyed by code here,
    // since the exact wording of the message it lands with is unknown.
    mockFetch(
      backend({
        'GET /api/v1/imports/presets': () => jsonResponse(PRESETS),
        'POST /api/v1/imports/archive': () =>
          jsonResponse(
            { detail: 'export.zip: damaged inside the zip (Bad CRC-32)', code: 'damaged' },
            422,
          ),
      }),
    )
    await renderApp('/imports')
    await chooseArchive(zipFile('export.zip'))
    await screen.findByText('Recognized: a LinkedIn data archive')

    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("didn't come through in one piece")
    expect(screen.getByText(/[Dd]ownload the export again/)).toBeVisible()
    expect(alert).not.toHaveTextContent(/uploading the zip LinkedIn emailed you/)
  })
})

describe('the "getting your data" panel (P1-21 item 3)', () => {
  // The whole reason this PR exists, per review finding 2: nothing asserted
  // a word of it before, so the menu path, the "ask for the full archive"
  // advice, the timing, and "nothing to unzip first" could all be deleted or
  // silently reworded.
  it('explains how to request the export from LinkedIn and which file to pick by hand', async () => {
    mockFetch(backend({ 'GET /api/v1/imports/presets': () => jsonResponse(PRESETS) }))
    await renderApp('/imports')
    await screen.findByText('Choose a file to import')

    expect(screen.getByText('Getting your data from LinkedIn')).toBeVisible()
    expect(screen.getByText('Settings & Privacy')).toBeVisible()
    expect(screen.getByText('Data privacy')).toBeVisible()
    expect(screen.getByText('Get a copy of your data')).toBeVisible()
    expect(screen.getByText(/Ask for your full data archive, not just Connections/)).toBeVisible()
    expect(screen.getByText(/budget for 1 to 24 hours/)).toBeVisible()
    expect(screen.getByText(/nothing to unzip first/i)).toBeVisible()

    expect(screen.getByText('Already unzipped it by hand?')).toBeVisible()
    // The sentence has `Connections.csv`/`messages.csv`/`Invitations.csv` as
    // their own `<strong>` elements, so it is matched as one block of text
    // by its full content rather than by a substring `getByText` alone can't
    // see across element boundaries.
    const byHand = screen.getByText(
      (_text, element) =>
        element?.tagName === 'P' &&
        (element.textContent ?? '').includes('Picking through the extracted files yourself') &&
        (element.textContent ?? '').includes('Connections.csv') &&
        (element.textContent ?? '').includes('messages.csv') &&
        (element.textContent ?? '').includes('Invitations.csv'),
    )
    expect(byHand).toBeVisible()
  })
})
