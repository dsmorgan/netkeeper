import type { ReactNode } from 'react'

/** A dense label/value list. Values that are null or undefined print as a dash. */
export function Facts({ items }: { items: ReadonlyArray<[label: string, value: ReactNode]> }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
      {items.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="min-w-0 truncate">{value ?? '—'}</dd>
        </div>
      ))}
    </dl>
  )
}
