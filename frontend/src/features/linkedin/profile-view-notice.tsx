/**
 * The profile-view notice `GET /linkedin/budget` returns as `profile_view_notice`
 * (#325): enrichment opens each contact's profile from your account, so they
 * may see a visit in Who viewed your profile. It informs and never blocks, and
 * it is not a risk warning, so it reads as a plain note. The sentence comes from
 * the server, so the CLI, every dialog and the docs use the same words.
 */
export function ProfileViewNotice({ text }: { text: string | null | undefined }) {
  if (text == null || text === '') return null
  return (
    <p
      role="note"
      aria-label="Profile views"
      className="rounded-lg bg-muted px-3 py-2 text-muted-foreground"
    >
      {text}
    </p>
  )
}
