import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

/**
 * A card's title, as a real `<h2>` rather than `CardTitle`'s plain
 * `<div data-slot="card-title">` (`components/ui/card.tsx`).
 *
 * This page is a dashboard of independent sections — scheduled runs, the
 * browser, budget, heat, runs, pins — and each one is a heading a screen
 * reader can jump to, which a `<div>` styled to look like one is not
 * (accessibility: "headings are real headings"). Same look as `CardTitle`,
 * so it drops into the same `CardHeader` without changing anything visually.
 */
export function SectionTitle({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <h2
      className={cn(
        'font-heading text-base leading-snug font-medium group-data-[size=sm]/card:text-sm',
        className,
      )}
    >
      {children}
    </h2>
  )
}
