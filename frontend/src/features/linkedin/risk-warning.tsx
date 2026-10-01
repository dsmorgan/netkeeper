/**
 * The profile-visit risk warning `GET /linkedin/budget` returns as
 * `risk_warning` when the daily limit is above 100 (#318). It informs and
 * never blocks: the sentence comes from the server, so every place shows the
 * same words `netkeeper posture` and `serve`'s startup log use.
 */
export function RiskWarning({ text }: { text: string | null | undefined }) {
  if (text == null || text === '') return null
  return (
    <p
      role="note"
      aria-label="Profile-visit risk"
      className="rounded-lg bg-amber-500/10 px-3 py-2 text-amber-800 dark:text-amber-300"
    >
      {text}
    </p>
  )
}
