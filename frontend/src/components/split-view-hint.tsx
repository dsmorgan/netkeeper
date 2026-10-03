/** Google's own help page for Chrome's split view. */
export const SPLIT_VIEW_HELP_URL = 'https://support.google.com/chrome/answer/16971124'

/**
 * Tells you how to keep a guide and the page it links to side by side. A web page
 * can't ask Chrome for a split view, and netkeeper never launches or drives your
 * browser, so the guidance is text. Put it once near the top of any guide whose
 * links open outside netkeeper, and open those links in a new tab
 * (`target="_blank" rel="noopener noreferrer"`).
 */
export function SplitViewHint() {
  return (
    <details className="text-muted-foreground">
      <summary className="cursor-pointer">See this guide and the linked page side by side</summary>
      <div className="mt-1 space-y-1">
        <p>
          In Chrome, right-click a link and choose <strong>Open link in split view</strong>. Or
          right-click the tab of the page you opened and choose{' '}
          <strong>New split view with current tab</strong>, or press{' '}
          <kbd className="font-mono">Cmd+Option+N</kbd> on macOS (
          <kbd className="font-mono">Shift+Alt+N</kbd> on Windows and Linux). To leave, choose the
          split view icon next to the address bar, then <strong>Separate split view</strong>. See{' '}
          <a
            href={SPLIT_VIEW_HELP_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="font-medium text-primary underline underline-offset-4"
          >
            Google’s split view help
          </a>
          .
        </p>
        <p>
          In another browser, drag the linked page’s tab out into its own window, then place the two
          windows side by side.
        </p>
      </div>
    </details>
  )
}
