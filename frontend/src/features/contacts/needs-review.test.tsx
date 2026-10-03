import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { NeedsReviewNotice } from './needs-review'

describe('NeedsReviewNotice (#364 N5)', () => {
  it('sends one answer per click until the write settles, then takes another', async () => {
    let settle: () => void = () => undefined
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve
        }),
    )
    render(
      <NeedsReviewNotice
        name="Rosalind Quillfeather"
        archived={false}
        pending={false}
        onConfirm={onConfirm}
        onReject={vi.fn()}
      />,
    )
    const confirm = screen.getByRole('button', { name: 'Confirm Rosalind Quillfeather' })

    fireEvent.click(confirm)
    fireEvent.click(confirm)
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(confirm).toBeDisabled()

    await act(async () => {
      settle()
      await Promise.resolve()
    })
    expect(confirm).toBeEnabled()
    fireEvent.click(confirm)
    expect(onConfirm).toHaveBeenCalledTimes(2)
  })

  it('re-enables after a failed write, leaving the error to the caller', async () => {
    const onReject = vi.fn(() => Promise.reject(new Error('409')))
    render(
      <NeedsReviewNotice
        name="Tobias Marrowbone"
        archived={false}
        pending={false}
        onConfirm={vi.fn()}
        onReject={onReject}
      />,
    )
    const reject = screen.getByRole('button', {
      name: 'Reject Tobias Marrowbone and archive the contact',
    })

    await act(async () => {
      fireEvent.click(reject)
      await Promise.resolve()
    })

    expect(reject).toBeEnabled()
    fireEvent.click(reject)
    expect(onReject).toHaveBeenCalledTimes(2)
  })
})
