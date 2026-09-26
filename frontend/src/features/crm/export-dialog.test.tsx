/**
 * The export dialog: every preset and format, and the two promises it must not make.
 */
import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'

import { ExportDialog, ExportForm } from './export-dialog'
import { EXPORT_PRESETS } from './export-presets'
import { mockApi, renderWithClient } from './harness'
import type { ExportFormat, ExportPreset, FilterTree } from './types'

const FILTER: FilterTree = { where: { op: 'has_email' }, include_archived: false }

function countRoute(total = 40) {
  return {
    'POST /api/v1/contacts/query': () => jsonResponse({ items: [], total, describe: 'has email' }),
  }
}

function downloadHref(): string {
  return screen.getByTestId('export-download').getAttribute('href') ?? ''
}

function choose(preset: ExportPreset, format: ExportFormat) {
  fireEvent.change(screen.getByLabelText('Preset'), { target: { value: preset } })
  fireEvent.change(screen.getByLabelText('Format'), { target: { value: format } })
}

describe('the export form', () => {
  it('offers the four presets and the three formats', () => {
    mockApi(countRoute())
    renderWithClient(<ExportForm filter={FILTER} />)
    expect(screen.getByLabelText('Preset')).toHaveDisplayValue('Nine-column')
    const presets = Array.from(screen.getByLabelText('Preset').querySelectorAll('option')).map(
      (option) => option.value,
    )
    expect(presets).toEqual(['nine-column', 'linkedin-archive', 'full', 'campaign-audience'])
    const formats = Array.from(screen.getByLabelText('Format').querySelectorAll('option')).map(
      (option) => option.value,
    )
    expect(formats).toEqual(['csv', 'json', 'vcard'])
  })

  it('builds the download URL for every preset and format', () => {
    mockApi(countRoute())
    renderWithClient(<ExportForm filter={FILTER} />)

    const presets: ExportPreset[] = EXPORT_PRESETS.map((preset) => preset.value)
    const formats: ExportFormat[] = ['csv', 'json', 'vcard']
    for (const preset of presets) {
      for (const format of formats) {
        choose(preset, format)
        const url = new URL(downloadHref(), 'http://localhost')
        expect(url.pathname).toBe('/api/v1/exports')
        expect(url.searchParams.get('preset')).toBe(preset)
        expect(url.searchParams.get('format')).toBe(format)
        expect(JSON.parse(url.searchParams.get('filter') ?? '{}')).toEqual(FILTER)
      }
    }
  })

  it('sends headerless only for CSV, and says so for the others', () => {
    mockApi(countRoute())
    renderWithClient(<ExportForm filter={FILTER} />)

    fireEvent.click(screen.getByRole('checkbox', { name: /leave out the header row/i }))
    expect(new URL(downloadHref(), 'http://localhost').searchParams.get('headerless')).toBe('true')

    fireEvent.change(screen.getByLabelText('Format'), { target: { value: 'json' } })
    expect(new URL(downloadHref(), 'http://localhost').searchParams.get('headerless')).toBeNull()
    expect(screen.getByText(/apply to CSV only/)).toBeInTheDocument()
  })

  it('sends spreadsheet_safe only when ticked, only for CSV, and says the file will not re-import', () => {
    mockApi(countRoute())
    renderWithClient(<ExportForm filter={FILTER} />)
    const param = () =>
      new URL(downloadHref(), 'http://localhost').searchParams.get('spreadsheet_safe')

    expect(param()).toBeNull()
    expect(
      screen.getByText(/Safe to open in a spreadsheet, not safe to re-import/),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: /safe to open in a spreadsheet/i }))
    expect(param()).toBe('true')

    fireEvent.change(screen.getByLabelText('Format'), { target: { value: 'vcard' } })
    expect(param()).toBeNull()
    expect(screen.queryByRole('checkbox', { name: /safe to open in a spreadsheet/i })).toBeNull()
  })

  it('exports every contact when there is no filter', () => {
    mockApi(countRoute())
    renderWithClient(<ExportForm filter={null} />)
    expect(new URL(downloadHref(), 'http://localhost').searchParams.get('filter')).toBeNull()
  })

  it('warns that nine-column round-trips the file, not the contact', () => {
    mockApi(countRoute())
    renderWithClient(<ExportForm filter={FILTER} />)
    expect(
      screen.getByText(/The file round-trips; a contact with two names does not/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/exports as “Bob” and reimports with both names set to “Bob”/),
    ).toBeInTheDocument()
  })

  it('explains why campaign-audience counts fewer people than the list does', () => {
    mockApi(countRoute())
    renderWithClient(<ExportForm filter={FILTER} listCount={40} />)
    fireEvent.change(screen.getByLabelText('Preset'), { target: { value: 'campaign-audience' } })

    expect(screen.getByText(/Everyone marked do-not-contact is left out/)).toBeInTheDocument()
    expect(screen.getByText(/that gap is the point, not a miscount/)).toBeInTheDocument()
    expect(
      screen.getByText(/This list counts 40 contacts; the file will hold that many/),
    ).toBeInTheDocument()
  })

  it('says linkedin-archive drops rows too, like nine-column', () => {
    mockApi(countRoute())
    renderWithClient(<ExportForm filter={FILTER} />)
    fireEvent.change(screen.getByLabelText('Preset'), { target: { value: 'linkedin-archive' } })
    expect(
      screen.getByText(/Contacts with nothing identifying in these columns are left out/),
    ).toBeInTheDocument()
  })

  it.each([
    ['nine-column', true],
    ['linkedin-archive', true],
    ['campaign-audience', true],
    ['full', false],
  ] as const)(
    'qualifies the headline count for %s only when it can drop rows',
    async (preset, drops) => {
      mockApi(countRoute(214))
      renderWithClient(<ExportForm filter={FILTER} />)
      fireEvent.change(screen.getByLabelText('Preset'), { target: { value: preset } })
      const headline = (await screen.findByText('214')).closest('[role="status"]')
      expect(headline).toHaveTextContent('214 contacts selected')
      if (drops) expect(headline).toHaveTextContent('The file may hold fewer')
      else expect(headline).not.toHaveTextContent('may hold fewer')
    },
  )

  it('makes no claim about the presets that carry no caveat', () => {
    mockApi(countRoute())
    renderWithClient(<ExportForm filter={FILTER} />)
    fireEvent.change(screen.getByLabelText('Preset'), { target: { value: 'full' } })
    expect(screen.queryByText('Before you pick this one')).toBeNull()
  })

  it('shows the count of what will be exported, and survives a count that fails', async () => {
    mockApi(countRoute(214))
    const { unmount } = renderWithClient(<ExportForm filter={FILTER} />)
    expect(await screen.findByText('214')).toBeInTheDocument()
    unmount()

    mockApi({
      'POST /api/v1/contacts/query': () => jsonResponse({ detail: 'nope' }, 500),
    })
    renderWithClient(<ExportForm filter={FILTER} />)
    expect(await screen.findByText(/the export will still run/)).toBeInTheDocument()
    expect(screen.getByTestId('export-download')).toBeInTheDocument()
  })
})

describe('an unfinished filter', () => {
  it('offers no download, because the server would answer 422 into a blank tab', () => {
    mockApi(countRoute())
    renderWithClient(
      <ExportForm filter={{ include_archived: false, where: { op: 'tag_any', names: [] } }} />,
    )
    expect(screen.queryByTestId('export-download')).toBeNull()
    expect(screen.getByText('Nothing to download yet')).toBeInTheDocument()
    expect(screen.getByText(/needs at least one tag/)).toBeInTheDocument()
  })
})

describe('the export dialog', () => {
  it('opens from a list and names it', async () => {
    mockApi(countRoute())
    renderWithClient(<ExportDialog filter={FILTER} listName="First 100" listCount={40} />)

    fireEvent.click(screen.getByRole('button', { name: /export/i }))
    expect(await screen.findByText('Export “First 100”')).toBeInTheDocument()
    expect(screen.getByLabelText('Preset')).toBeInTheDocument()
  })
})
