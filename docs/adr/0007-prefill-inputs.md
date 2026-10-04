# 0007. The prefill's inputs: one Message click, typing into one verified composer, never Enter

Date: 2026-10-03

## Status

Proposed

This is a draft. It becomes Accepted only after the P4-06 capture (#374) is analyzed and merged, every placeholder below is replaced with what the capture showed, and the maintainer accepts it in review.

Placeholders look like this: **TBD(#374): what to confirm.** Each one is a fact that only the live capture can settle. None of them is a guess, and none may be filled in from memory, from another project, or from LinkedIn's help pages. The [list at the end](#facts-to-confirm-after-the-capture) collects them.

## Context

[ADR 0004](0004-manual-linkedin-sends-by-default.md) says the LinkedIn step's default mode is `prefill`: the sidecar opens the conversation in the user's Chrome, types the rendered message, and stops, and the user clicks Send. It does not say how that typing is made safe.

[ADR 0006](0006-observe-dont-request.md) says the only input netkeeper gives a LinkedIn page is navigation, the scroll replay (with its pointer rest, #192), and one click on **Contact info** per profile visit (#190). `tests/test_browser_safety.py` enforces that: `INPUT_CALLS` names every click, key, tap, hover, typing, focus, and synthetic event, and `ALLOWED_INPUTS` allows exactly one `click` (`BrowserRun.click_contact_info`) and one `move` (`BrowserRun._rest_pointer_over_content`) in the extractor. A prefill can't be built under those rules, and it shouldn't be built by loosening them case by case in a code review.

Three risks shape the exceptions:

- **A sent message can't be taken back.** A key that sends, or a click on Send, turns a prefill into an automated message: the thing ADR 0004 exists to prevent, and what LinkedIn restricts accounts for fastest. LinkedIn has a "Press Enter to Send" setting, so whether Enter sends depends on a setting netkeeper doesn't control.
- **Typing into the wrong place is worse than not typing.** A composer for another person, a composer that already holds a draft, or a second composer left open by an earlier visit would each put the body somewhere the user didn't intend.
- **The tab is the user's after the prefill.** The user has to find the typed message and click Send. A run that later reused or closed that tab would destroy the draft.

The maintainer's decisions of 2026-10-03 (#375, and the phase 4 decisions in `docs/implementation-guide.md`) settle the shape:

- A person triggers each prefill. Due LinkedIn steps wait in a "ready to prefill" queue, and the person clicks **Prefill** while watching Chrome. Nothing prefills on a schedule.
- The composer is opened by navigating to the contact's profile and clicking **Message**.
- Newlines are typed as Shift+Enter only if P4-06 (#374) shows that Shift+Enter never sends, whatever the "Press Enter to Send" setting is. Otherwise lint refuses multi-line LinkedIn templates (P4-11, #377), and the prefill never presses any key.
- The prefill releases the browser lock after typing. The handed-over tab stays open and is brought to the front. netkeeper never closes it.
- Auto-send (P4-04, #384) is built only after CP8 signs off, and needs its own amendment.

## Decision

netkeeper may give a LinkedIn page exactly two new inputs, both in `BrowserRun` (`netkeeper/linkedin/browser.py`), and one new way to end a run. Everything else in ADR 0006 stays: no script in the page, no request of netkeeper's own, and no interception.

### One click on Message

`BrowserRun.click_message` clicks the **Message** control once per prefill, on the contact's profile, modeled on `BrowserRun.click_contact_info`:

- It refuses when the run's tab is gone, and when the tab isn't on the contact's profile path.
- It pauses the way a person does before reaching for the control.
- It finds the control by its accessible role and name. **TBD(#374): the Message control's role and exact accessible name**, and whether the visible text and the accessible name differ.
- It refuses unless the control is the only match in the profile's top card. A second **Message** control elsewhere on the page doesn't count as a match but must never be the one clicked. **TBD(#374): every Message control a profile shows** (the top card, the sticky header that appears on scroll, and cards such as "People also viewed"), **and how the top card's control is told apart from the others** by role, name, and landmark, never by a CSS class.
- It refuses unless the control names this contact: an `href` or a payload that carries the contact's URN or public id, checked the way `click_contact_info` checks `overlay/contact-info/`. **TBD(#374): whether the control is a link or a button, and which attribute, if any, names the profile.** If no attribute on the control names the profile, this ADR must be revised before acceptance, because the click can't then be bound to the contact.
- It clicks once, at the control's own box, with a person's press length. A click that fails isn't tried again.

**TBD(#374): what the click opens**: a message bubble at the bottom of the profile page, or a navigation to `/messaging/…`. The checks that follow are the same either way, but the page the composer is verified on, and what P4-03's replica copies, depend on it.

### Typing into one verified composer

`BrowserRun.type_into_composer` types the rendered body into the composer the click opened. Before the first key, it verifies all three of these, and types nothing if any fails:

- **Exactly one composer is open.** **TBD(#374): the composer's role and accessible name**, and the selector P4-03 uses to find it. Like the Message control, it's found by role and name, never by a CSS class.
- **The composer is empty.** A composer that holds any text, including a draft LinkedIn restored on its own, is refused. **TBD(#374): whether LinkedIn keeps drafts server-side** and restores them into a new composer, and whether an open bubble and its draft persist across pages (#374, step 12). If drafts do persist, the prefill refuses, and the person clears the draft.
- **The composer's recipient is this contact**, checked two ways:
  - from what the page loaded: the conversation's participant `urn:li:fsd_profile:<id>`, read from the answer the page itself received, through `BrowserRun.observe` (ADR 0006). **TBD(#374): which request carries the conversation and where its participant URN sits.**
  - from the composer's accessible label. **TBD(#374): how the composer shows its recipient** (its accessible name, a heading in the bubble, or neither).

**TBD(#374): whether the composer has keyboard focus after the Message click.** This draft authorizes no click into the composer and no `focus` call. If the capture shows that focus doesn't land in the composer on its own, this ADR must be revised before acceptance to name that input, with its own checks; P4-03 must not add one under this ADR as written.

Typing replays the plan from `pacing.typing_plan` (P4-10, #376): lognormal delays, median 140 ms a character, no typos, a 300-second ceiling. Each character is typed with `keyboard.type`. A chunk `keyboard.type` can't send, such as an emoji, is sent with `keyboard.insert_text`. The run checks for cancel between chunks, and stops when the tab closes or its URL changes.

**TBD(#374): the requests the page sends while a person types** (typing indicators, draft saves). These are the page's own requests and ADR 0006 allows them; netkeeper sends nothing of its own. They do mean the recipient may see a typing indicator while the prefill types, and the ADR should say so once the capture confirms it.

### Never Enter, never Send

The prefill never presses bare Enter, NumpadEnter, Ctrl+Enter, or Cmd+Enter, and never clicks Send.

The only key it may ever press is Shift+Enter, for a newline, and only if P4-06 shows that Shift+Enter never sends, with the "Press Enter to Send" setting on and with it off. **TBD(#374): the Enter and Shift+Enter table from step 9**:

| Setting | Key | New line or sent? |
|---|---|---|
| On | Shift+Enter | TBD(#374) |
| On | Enter | TBD(#374) |
| Off | Shift+Enter | TBD(#374) |
| Off | Enter | TBD(#374) |
| Off | ⌘+Enter | TBD(#374) |

If either Shift+Enter row says "sent", or the capture is unclear, the prefill presses no key at all, P4-11's lint refuses multi-line LinkedIn templates, and this section is rewritten to say so before acceptance.

The Send click is not authorized here. Auto-send (P4-04, #384) needs its own amendment, gated on `[campaigns] linkedin_auto_send` and the step's `auto_send` mode, and is built only after CP8 signs off.

### Handing the tab over

At the end of a prefill run (spec 11.6), `BrowserRun.hand_over()` lets go of the connection without closing the tab and brings the tab to the front. From then on the tab isn't netkeeper's: no later run reuses it, navigates it, or closes it, and `close()` is never called on it. `hand_over` is a new ending, distinct from `close()`, and is reached only from `netkeeper/linkedin/page_messaging.py`. The run releases the browser lock after typing; it doesn't wait for the send. The next inbox poll, or the "I sent it, check now" action, confirms it.

### Who triggers it

A prefill runs only when a person asks for it, from the "ready to prefill" queue, while watching Chrome. `create_run` refuses a scheduled `message_send` run (P4-03, #382).

### The pins

P4-03 (#382) changes `tests/test_browser_safety.py` in the same pull request as the code these pins guard, not in this one:

- Exactly two new `ALLOWED_INPUTS` entries, by method: `BrowserRun.click_message` for `click`, and `BrowserRun.type_into_composer` for `type`, `insert_text`, and `press`.
- A literal check that the only `press` argument anywhere is `"Shift+Enter"`, or that there is no `press` at all if newlines are refused.
- No `click` on anything named Send.
- `netkeeper/linkedin/page_messaging.py` in `BROWSER_MODULES` and `BROWSER_CALLERS`.
- `hand_over` reachable only from `page_messaging.py`.

## Consequences

- netkeeper can type a message into LinkedIn and still never send one. What the user sees in the handed-over tab is the rendered body, in the right conversation, waiting for their click.
- Every refusal before the first key leaves nothing typed (`not_typed`), so a refused prefill costs a budget unit, not a wrong message. A prefill interrupted after the first key (`partially_typed`, `unknown`) is never retried: retyping into a composer that may hold half the message is worse than a person finishing it.
- Each prefill spends one `li_prefills` unit and one `profile_visits` unit before it navigates (P4-03, P4-09).
- Handed-over tabs accumulate in the user's Chrome until the user closes them. That's deliberate: netkeeper closing a tab is how a draft would be lost. P4-09 allows one open prefill at a time.
- If LinkedIn changes the Message control, the composer, or the recipient it shows, the prefill refuses and types nothing. That is the intended failure.
- If LinkedIn changes what Shift+Enter does, nothing in this ADR detects it at run time. The capture is the evidence, and a later capture that contradicts it means newlines are refused until a new amendment.
- `keyboard` is itself a name in `INPUT_CALLS`, so `page.keyboard.type(...)` is read as two inputs, `keyboard` and `type`. When P4-03 writes the pins, it either lists `keyboard` for `type_into_composer` as well, or the maintainer decides how the scanner reads it. This ADR doesn't widen the input list beyond the methods above.
- A future contributor keeps these true: the only clicks are Contact info and Message, each once, each bound to the profile it's on; the only typing goes into one verified, empty composer for the contact; no code path presses Enter or clicks Send; and a handed-over tab is never touched again.

## Facts to confirm after the capture

Each item is a placeholder above. P4-06's analysis (`docs/linkedin-messaging-shapes.md`) answers it, and this ADR is updated before it's accepted.

1. The Message control's role and accessible name, and its visible text.
2. Every Message control on a profile, and how the top card's is told apart.
3. Whether the Message control is a link or a button, and what on it names the profile.
4. What the Message click opens: a bubble on the profile page, or `/messaging/…`.
5. The composer's role and accessible name, and the selector P4-03 uses.
6. How the composer shows its recipient.
7. Which request carries the conversation, and where its participant URN sits.
8. Whether the composer has focus after the Message click.
9. The Enter and Shift+Enter table, and whether Shift+Enter never sends whatever the setting is.
10. Requests the page sends while a person types (typing indicators, draft saves).
11. Whether LinkedIn keeps drafts server-side, and whether a bubble and its draft persist across pages.
