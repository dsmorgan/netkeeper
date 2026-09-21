/**
 * A render counter for the Contacts table's rows.
 *
 * "The table stays responsive at 10,000 rows" (P1-12) is a claim about work
 * done, so it is measured rather than eyeballed: `src/test/contacts-performance.test.tsx`
 * reads this counter to prove that typing in the filter box re-renders no rows,
 * and that a long page renders a window instead of every row. The cost in the
 * app is one integer increment per row render.
 */
export const rowRenders = { count: 0 }

export function resetRowRenders(): void {
  rowRenders.count = 0
}
