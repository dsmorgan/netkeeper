export { cn } from 'cn'

/** A search term as a filter chip says it: quoted, so it reads as typed (#402). */
export function quoted(term: string): string {
  return `“${term}”`
}
