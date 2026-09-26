import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Card, CardHeader, CardTitle } from './card'

describe('CardTitle', () => {
  it.each([2, 3, 4] as const)('is a real level-%i heading', (level) => {
    render(
      <Card>
        <CardHeader>
          <CardTitle level={level}>Auto-tag rules</CardTitle>
        </CardHeader>
      </Card>,
    )
    const heading = screen.getByRole('heading', { name: 'Auto-tag rules', level })
    expect(heading.tagName).toBe(`H${level}`)
    // The slot the card's layout and the tests key on is kept.
    expect(heading).toHaveAttribute('data-slot', 'card-title')
  })
})
