# 0006. Observe, don't request

Date: 2026-09-24

## Status

Accepted

## Context

Spec 9.3 said netkeeper reads LinkedIn through its internal Voyager API, from inside the page: an in-page `fetch` to `/voyager/api/...` with the session's cookies and a `csrf-token` header. The first supervised sync (#31) sent one such request, got an answer without `elements`, and the DOM fallback then found nothing. The capture that followed (#149, 2026-09-24) explained why:

- The connections list and profiles no longer use Voyager. They use `flagship-web`, a React Server Components client. The connections page carries its first ten cards in its own HTML, and as a person scrolls, the page itself sends `POST /flagship-web/rsc-action/actions/pagination` and receives the next ten. The capture has no `relationships/dash/connections` call at all. Profiles load as `POST /flagship-web/in/<slug>/`, and the contact details arrive in `POST /flagship-web/rsc-action/actions/navigation` after a person clicks **Contact info**.
- Bot detection runs on these pages: PerimeterX sensors, reCAPTCHA Enterprise, and obfuscated telemetry posts. A request the page did not make is exactly what those look for: an in-page `fetch` of an endpoint the page no longer calls, with a header set the real client does not send, at a moment no scroll explains.
- The real client paces itself. In the capture, pagination answers arrive a quarter to half a second after the request, and the requests come several seconds apart, as the reading scroll allows. netkeeper's own guesses at that pacing (#149, item 7) are unnecessary once the page decides when to ask.

## Decision

netkeeper drives the page and reads what the page loads. It navigates its own tab, scrolls it with `BrowserRun.scroll`'s wheel replay, and reads the responses to requests the page itself sent on that tab. That reading is passive and read-only:

- **No interception.** No `route`, no `unroute`, nothing that holds a request, changes it, answers it, or cancels it (`continue_`, `fulfill`, `abort`), no header overrides. A response listener sees an answer after the browser already has it.
- **No requests of netkeeper's own.** No in-page `fetch`, and no Playwright API request context. Every request LinkedIn sees on netkeeper's tab is one its own page decided to send.
- **The seam is one method.** `BrowserRun.observe(match)` listens to the tab's `response` events and keeps the bodies of the responses whose method, origin, and path the match names, in arrival order, with bounded memory and a body timeout. `PageLike` stays narrow; the listener methods are borrowed by that one method. Bodies never reach a log.
- **Pages are parsed whole or not at all.** A flight payload the parser does not recognize is `RouteChanged`. A page of connections is handed on only when every card on it parsed, so a shape change can stop a run but can never write part of a page or put one person's details on another's URN.
- **Completeness is proven by the page.** The end of the connections list is an answer the page received that says so (no cards, or a short page that asks for no next one), and a full sync is complete only when it also saw at least as many people as the total the page states. A page that stops loading proves nothing.

**One exception: the Contact info click.** The contact details only arrive when a person clicks **Contact info**; there is no page load that carries them. Enrichment may click that one control, once per profile visit, paced and budgeted as part of the visit (it spends no extra `profile_visits` unit and is covered by `contact_info_fetches`, which spec 9.6 already ties to visits). No other click is allowed. The click will be a narrow `BrowserRun` method of its own, added by the enrichment lane, and the modules that read the page's answers never click, type, or evaluate script themselves (`tests/test_browser_safety.py`). This amends ADR 0002's "scroll is the only automation" reading of spec 9.1 for that one control.

## Consequences

- LinkedIn sees netkeeper's tab behave like a person's: a page load, scrolling, and the page's own requests at the page's own pace. There is no request for bot detection to single out that a person's browser would not also send.
- netkeeper can only read what the page chooses to load. A field the page does not render is out of reach, and a page that changes when it loads data (an infinite scroll that becomes a "Show more" button) stops the sync as `RouteChanged` until the parser or the scroll is updated. That is the intended failure: a run stops and ages nobody.
- The first screen's total and the page's end-of-list answer are what make a full sync complete. If the total counts a member the list never shows, no full sync completes and nobody ages. That is the safe direction; the first supervised run checks it.
- Budgets still count units of work, not requests: one `connection_pages` unit reads about 40 connections, which the page loads in about four of its own answers.
- The in-page Voyager fetch is retired for connections. Enrichment still uses it until the enrichment lane moves profiles and contact info onto the same seam, which is also when the Contact info click is added.
- P2-08's DOM fallback is no longer wired: its selectors were authored, never seen on the live page, and found nothing on the first supervised run. Reading the page's own answers replaces it rather than falling back to it.
- A future contributor keeps three things true: nothing routes or alters a request; nothing sends one netkeeper wrote; and the only input netkeeper gives a LinkedIn page is navigation, the scroll replay, and the one Contact info click.
