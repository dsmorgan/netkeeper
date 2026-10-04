# 0007. The prefill's inputs: one Message click, typing into one verified composer, never Enter

Date: 2026-10-03

## Status

Proposed

This is a draft. It becomes Accepted only after three things happen: the P4-06 capture (#374) is analyzed and merged, every placeholder below is replaced with what the capture showed, and the maintainer makes the [decisions listed for acceptance](#decisions-the-maintainer-makes-at-acceptance) and accepts it in review.

Placeholders look like this: **TBD(#374): what to confirm.** Each one is a fact that only the live capture can settle. None of them is a guess, and none may be filled in from memory, from another project, or from LinkedIn's help pages. The [list at the end](#facts-to-confirm-after-the-capture) collects them. A **TBD(maintainer)** is a choice, not a fact, and is listed with the decisions for acceptance.

## Context

[ADR 0004](0004-manual-linkedin-sends-by-default.md) says the LinkedIn step's default mode is `prefill`: the sidecar opens the conversation in the user's Chrome, types the rendered message, and stops, and the user clicks Send. It does not say how that typing is made safe.

[ADR 0006](0006-observe-dont-request.md) says the only input netkeeper gives a LinkedIn page is navigation, the scroll replay (with its pointer rest, #192), and one click on **Contact info** per profile visit (#190). `tests/test_browser_safety.py` enforces that: `INPUT_CALLS` names every click, key, tap, hover, typing, focus, and synthetic event, and `ALLOWED_INPUTS` allows exactly one `click` (`BrowserRun.click_contact_info`) and one `move` (`BrowserRun._rest_pointer_over_content`) in the extractor. A prefill can't be built under those rules, and it shouldn't be built by loosening them case by case in a code review.

Three risks shape the exceptions:

- **A sent message can't be taken back.** A key that sends, or a click on Send, turns a prefill into an automated message: the thing ADR 0004 exists to prevent, and what LinkedIn restricts accounts for fastest. #374 expects a "Press Enter to Send" setting (**TBD(#374)**); if it exists, whether Enter sends depends on a setting netkeeper doesn't control.
- **Typing into the wrong place is worse than not typing.** A composer for another person, a composer that already holds a draft, or a second composer left open by an earlier visit would each put the body somewhere the user didn't intend. So would keys that keep arriving after focus has moved somewhere else.
- **The tab is the user's after the prefill.** The user has to find the typed message and click Send. A run that later reused or closed that tab would destroy the draft.

The maintainer's decisions of 2026-10-03 (#375, and the phase 4 decisions in `docs/implementation-guide.md`) settle the shape:

- A person triggers each prefill. Due LinkedIn steps wait in a "ready to prefill" queue, and the person clicks **Prefill** while watching Chrome. Nothing prefills on a schedule.
- The composer is opened by navigating to the contact's profile and clicking **Message**.
- Newlines are typed as Shift+Enter only if P4-06 (#374) shows that Shift+Enter never sends, whatever the "Press Enter to Send" setting is. Otherwise lint refuses multi-line LinkedIn templates (P4-11, #377), and the prefill never presses any key.
- The prefill releases the browser lock after typing. The handed-over tab stays open and is brought to the front. netkeeper never closes it.
- Auto-send (P4-04, #384) is built only after CP8 signs off, and needs its own amendment.

## Decision

netkeeper may give a LinkedIn page exactly two new inputs, both in `BrowserRun` (`netkeeper/linkedin/browser.py`), and one new way to end a run. Everything else in ADR 0006 stays: no script in the page, no request of netkeeper's own, and no interception. It also brings its tab to the front once, at the start (see below).

### Who triggers it

A prefill runs only when a person asks for it, from the "ready to prefill" queue, while watching Chrome. `create_run` refuses a scheduled `message_send` run (P4-03, #382).

A prefill run never waits for the browser lock: it asks for it with `wait=False`, and if another run holds it, the prefill doesn't start. A prefill run is never retried and never resumed. A claim that hasn't started within N seconds lapses back to the queue, so a prefill never starts long after the person who asked has stopped watching. **TBD(maintainer): N** (suggested: 60).

### Bringing the tab to the front

The run brings its tab to the front once, at the start, before `click_message`, while the person is watching. That is the only `bring_to_front` call in the package. Nothing later in the run changes focus, and `hand_over()` doesn't either: a focus change while the person is reading or typing elsewhere could send their keys somewhere they didn't intend.

This adjusts the maintainer's "brought to the front" wording, which placed it at the hand-over. The maintainer confirms it at acceptance.

### One click on Message

`BrowserRun.click_message` clicks the **Message** control once per prefill, on the contact's profile, modeled on `BrowserRun.click_contact_info`:

- It refuses when the run's tab is gone, and when the tab isn't on the contact's profile path.
- It pauses the way a person does before reaching for the control.
- It finds the control by its accessible role and name. **TBD(#374): the Message control's role and exact accessible name**, and whether the visible text and the accessible name differ. The role and name are module constants, pinned to literals.
- It looks for the control only inside the top card and refuses unless exactly one is there; controls elsewhere are never candidates. **TBD(#374): every Message control a profile shows** (the top card, the sticky header that appears on scroll, and cards such as "People also viewed"), **and how the top card is found** by role, name, and landmark, never by a CSS class.
- It refuses unless the control names this contact: an `href` or a payload that carries the contact's URN or public id, checked the way `click_contact_info` checks `overlay/contact-info/`. **TBD(#374): whether the control is a link or a button, and which attribute, if any, names the profile.** If no attribute on the control names the profile, this ADR must be revised before acceptance, because the click can't then be bound to the contact.
- It clicks once, at the control's own box, with a person's press length. A click that fails isn't tried again.

**TBD(#374): what the click opens**: a message bubble at the bottom of the profile page, or a navigation to `/messaging/…`. The checks that follow are the same either way, but the page the composer is verified on, and what P4-03's replica copies, depend on it.

### No reattach after the click

Once `click_message` returns, the run never calls `ensure_page` or `goto` and never reattaches. A lost tab or browser ends the run: `not_typed` before the first key, `unknown` after it. A reopened tab would be a new page with no verified composer, and a reattach could land keys in a tab nobody checked.

### Typing into one verified composer

`BrowserRun.type_into_composer` types the rendered body into the composer the click opened. Before the first key, it verifies all of these, and types nothing if any fails:

- **Exactly one composer is open.** **TBD(#374): the composer's role and accessible name**, and the selector P4-03 uses to find it. Like the Message control, it's found by role and name, never by a CSS class.
- **The composer is empty.** A composer that holds any text, including a draft LinkedIn restored on its own, is refused. **TBD(#374): whether LinkedIn keeps drafts server-side** and restores them into a new composer, and whether an open bubble and its draft persist across pages (#374, step 12). If drafts do persist, the prefill refuses, and the person clears the draft.
- **The composer's recipient is this contact**, checked two ways:
  - from what the page loaded: the conversation's participant `urn:li:fsd_profile:<id>`, read from the answer the page itself received, through `BrowserRun.observe` (ADR 0006). **TBD(#374): which request carries the conversation and where its participant URN sits.**
  - from the composer's accessible label. **TBD(#374): how the composer shows its recipient** (its accessible name, a heading in the bubble, or neither).

  Both checks must pass. The label check alone never authorizes typing. **TBD(#374): what the page loads when the contact has no conversation yet.** Until the capture shows it, no observed conversation means `not_typed`.
- **The verified composer holds focus.** See the next subsection.
- **No chunk holds a control character.** The method refuses, before the first key, a plan in which any chunk contains `\n`, `\r`, or any other C0 or C1 control character. Playwright's `keyboard.type` maps `\n` and `\r` to the Enter key, so a newline is only ever a `newline=True` step of the plan, pressed as Shift+Enter.

**TBD(#374): whether the composer has keyboard focus after the Message click.** This draft authorizes no click into the composer and no `focus` call. If the capture shows that focus doesn't land in the composer on its own, this ADR must be revised before acceptance to name that input, with its own checks; P4-03 must not add one under this ADR as written.

Typing replays the plan from `pacing.typing_plan` (P4-10, #376): lognormal delays, median 140 ms a character, no typos, a 300-second ceiling. Each character is typed with `keyboard.type`. A chunk `keyboard.type` can't send, such as an emoji, is sent with `keyboard.insert_text`. The run checks for cancel between chunks, and stops when the tab closes or its URL changes.

### Checked before every chunk

The checks above don't run only once. Playwright's `keyboard.type` sends keys to whatever has focus and doesn't check where that is, and it maps a space to the Space key. If focus moved mid-type, to a Send button for example, a Space or a Shift+Enter could send the message, and the rest of the body could land in the wrong place.

- Before every chunk and every Shift+Enter, `type_into_composer` checks again that exactly one composer is open, that its recipient label is unchanged, that the verified composer holds focus, and that its text equals the prefix typed so far. **TBD(#374): how focus is read without running script in the page.** If it can't be, this ADR must be revised before acceptance.
- Any failed check stops typing at once, with the outcome `partially_typed`.
- Before the hand-over, the composer's text must equal the body, or the outcome is `partially_typed`.
- The prefill UI tells the person not to type or click in Chrome while the prefill types.

**TBD(#374): the requests the page sends while a person types** (typing indicators, draft saves). These are the page's own requests and ADR 0006 allows them; netkeeper sends nothing of its own. If the recipient can see a typing indicator, they may see it for as long as the prefill types (up to the plan's 300-second ceiling), even if the person never sends. Before acceptance, the maintainer decides whether that's acceptable. If it is, the **Prefill** button's copy says so. If it isn't, the prefill is redesigned.

### Never Enter, never Send

The prefill never presses bare Enter, NumpadEnter, Ctrl+Enter, or Cmd+Enter, and never clicks Send.

The only key it may `press` is Shift+Enter, for a newline, and only if P4-06 shows that Shift+Enter never sends, with the "Press Enter to Send" setting on and with it off. The table must come from the same kind of composer the prefill uses (the bubble or the `/messaging/` page, whichever the Message click opens). **TBD(#374): the Enter and Shift+Enter table from step 9**, given that #374 expects a "Press Enter to Send" setting (**TBD(#374)**):

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

At the end of a prefill run (spec 11.6), `BrowserRun.hand_over()` drops the run's reference to its tab and detaches without closing it, so the provider's later `close()` closes nothing. It changes no focus (see [Bringing the tab to the front](#bringing-the-tab-to-the-front)). From then on the tab isn't netkeeper's: no later run reuses it, navigates it, or closes it. `hand_over` is a new ending, distinct from `close()`, and is reached only from `netkeeper/linkedin/page_messaging.py`. The run releases the browser lock after typing; it doesn't wait for the send. The next inbox poll, or the "I sent it, check now" action, confirms it.

Every outcome after the first key hands the tab over, not only `prefilled`. After a failure mid-type, the tab still holds what was typed:

- The UI flags `partially_typed` and `unknown` with "Part of a message is in the composer and may be saved as a draft. Clear it."
- netkeeper never clears it. Clearing is an input this ADR doesn't authorize.
- If the run crashes, the message becomes `unknown` and is never queued again.

### The pins

P4-03 (#382) changes `tests/test_browser_safety.py` in the same pull request as the code these pins guard, not in this one.

Static pins:

- `ALLOWED_INPUTS` entries for exactly two methods: `click` in `BrowserRun.click_message`; and `keyboard` (read once, into a local), `type`, `insert_text`, and `press` in `BrowserRun.type_into_composer`.
- A literal check that the only `press` argument anywhere is `"Shift+Enter"`, or that there is no `press` at all if newlines are refused.
- No `keyboard.down`, no `keyboard.up`, and no `focus()` anywhere.
- The Message control's role and name are module constants pinned to literals, and no role or name literal under `netkeeper/linkedin/` matches "send", ignoring case.
- `bring_to_front` is called at one site, inside the prefill's start, before `click_message`; a runtime pin checks that it's called once per run.
- `netkeeper/linkedin/page_messaging.py` in `BROWSER_MODULES` and `BROWSER_CALLERS`.
- `hand_over` reachable only from `page_messaging.py`.

Runtime pins, against a fake page:

- A plan with a `"\n"` chunk gives `not_typed` with zero keys.
- Focus moving away after chunk k gives zero further keys.
- Composer text that diverges from the typed prefix stops typing.
- A second composer appearing mid-type stops typing.
- A recipient label that changes mid-type stops typing.
- A final composer text that differs from the body gives `partially_typed`, never `prefilled`.
- After `hand_over()`, the provider's exit leaves the page open.

## Consequences

- netkeeper can type a message into LinkedIn, and no key or click of netkeeper's is meant to send one. What the user sees in the handed-over tab is the rendered body, in the right conversation, waiting for their click.
- Every refusal before the first key leaves nothing typed (`not_typed`), so a refused prefill costs a budget unit, not a wrong message. A prefill interrupted after the first key (`partially_typed`, `unknown`) is never retried: retyping into a composer that may hold half the message is worse than a person finishing it. The person clears what's there.
- Each prefill spends one `li_prefills` unit and one `profile_visits` unit before it navigates (P4-03, P4-09).
- A prefill that can't get the browser lock at once doesn't start, and a claim that waits too long lapses. A person may have to click **Prefill** again.
- Focus is checked before each chunk, not atomically with it. If focus moves in the moment between the check and the key, one chunk can land outside the composer. The per-chunk checks stop the prefill on the next chunk. This window is the remaining risk, and the reason the person watches and leaves Chrome alone.
- Handed-over tabs accumulate in the user's Chrome until the user closes them. That's deliberate: netkeeper closing a tab is how a draft would be lost. P4-09 allows one open prefill at a time.
- The person must leave Chrome alone while the prefill types. Touching it stops the prefill as `partially_typed`, which is the safe direction.
- If LinkedIn changes the Message control, the composer, or the recipient it shows, the prefill refuses and types nothing. That is the intended failure.
- If LinkedIn changes what Shift+Enter does, nothing in this ADR detects it at run time. The capture is the evidence, and a later capture that contradicts it means newlines are refused until a new amendment.
- A future contributor keeps these true: the only clicks are Contact info and Message, each once, each bound to the profile it's on; the only typing is aimed at one verified composer for the contact, with focus and the composer checked again before every chunk; no code path presses Enter or clicks Send; and a handed-over tab is never touched again.

## Decisions the maintainer makes at acceptance

1. **Where the tab comes to the front.** This draft brings it to the front once, at the run's start, and `hand_over()` changes no focus. That adjusts the "brought to the front" wording of 2026-10-03.
2. **The typing indicator.** Whether a typing indicator the recipient can see, for up to 300 seconds and even if the person never sends, is acceptable. If it is, the **Prefill** button's copy says so; if not, the prefill is redesigned.
3. **N, the claim lapse.** How many seconds a claim may wait to start before it lapses back to the queue (suggested: 60).

## Facts to confirm after the capture

Each item is a placeholder above. P4-06's analysis (`docs/linkedin-messaging-shapes.md`) answers it, and this ADR is updated before it's accepted.

1. The Message control's role and accessible name, and its visible text.
2. Every Message control on a profile, and how the top card is found.
3. Whether the Message control is a link or a button, and what on it names the profile.
4. What the Message click opens: a bubble on the profile page, or `/messaging/…`.
5. The composer's role and accessible name, and the selector P4-03 uses.
6. How the composer shows its recipient.
7. Which request carries the conversation, and where its participant URN sits.
8. Whether the composer has focus after the Message click, and how focus is read without running script in the page.
9. Whether a "Press Enter to Send" setting exists, and the Enter and Shift+Enter table, captured in the same kind of composer the prefill uses: does Shift+Enter never send, whatever the setting is?
10. Requests the page sends while a person types (typing indicators, draft saves), and whether the recipient sees a typing indicator.
11. Whether LinkedIn keeps drafts server-side, and whether a bubble and its draft persist across pages, tabs, and the mobile app.
12. What the page loads when the contact has no conversation yet.
