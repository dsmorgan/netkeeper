# 0007. The prefill's inputs: one Message click, typing into one verified composer, never Enter

Date: 2026-10-03, updated 2026-10-05 with the P4-06 messaging capture (#374)

## Status

Proposed

The maintainer accepts this ADR in review. Before acceptance, the maintainer makes the [decisions listed for acceptance](#decisions-the-maintainer-makes-at-acceptance), and P4-06's analysis (`docs/linkedin-messaging-shapes.md`, #374) merges first. The facts below come from the maintainer's structural summary of the 2026-10-05 capture (the last comment on #374). Where this ADR names a request field the summary doesn't spell out, it says so, and P4-03 (#382) takes the exact field from the shape doc.

## Context

[ADR 0004](0004-manual-linkedin-sends-by-default.md) says the LinkedIn step's default mode is `prefill`: the sidecar opens the conversation in the user's Chrome, types the rendered message, and stops, and the user clicks Send. It doesn't say how that typing is made safe.

[ADR 0006](0006-observe-dont-request.md) says the only input netkeeper gives a LinkedIn page is navigation, the scroll replay (with its pointer rest, #192), and one click on **Contact info** per profile visit (#190). `tests/test_browser_safety.py` enforces that: `INPUT_CALLS` names every click, key, tap, hover, typing, focus, and synthetic event, and `ALLOWED_INPUTS` allows exactly one `click` (`BrowserRun.click_contact_info`) and one `move` (`BrowserRun._rest_pointer_over_content`) in the extractor. A prefill can't be built under those rules, and it shouldn't be built by loosening them case by case in a code review.

Three risks shape the exceptions:

- **A sent message can't be taken back.** A key that sends, or a click on Send, turns a prefill into an automated message: the thing ADR 0004 exists to prevent, and what LinkedIn restricts accounts for fastest. The capture shows that LinkedIn has a **Press Enter to Send** setting, off by default ("Click Send"), and that whether Enter sends depends on it. netkeeper doesn't control that setting. With the setting off, ⌘+Enter sends.
- **Typing into the wrong place is worse than not typing.** A composer for another person, a composer that already holds a draft, or a second composer left open by an earlier visit would each put the body somewhere the user didn't intend. So would keys that keep arriving after focus has moved somewhere else. The capture shows that a minimized message bubble stays open across pages and keeps its draft, so an earlier bubble can still be on screen when a prefill starts.
- **The tab is the user's after the prefill.** The user has to find the typed message and click Send. A run that later reused or closed that tab would destroy the draft: closing a bubble deletes its draft.

The maintainer's decisions of 2026-10-03 (#375, and the phase 4 decisions in `docs/implementation-guide.md`) settle the shape:

- A person triggers each prefill. Due LinkedIn steps wait in a "ready to prefill" queue, and the person clicks **Prefill** while watching Chrome. Nothing prefills on a schedule.
- The composer is opened by navigating to the contact's profile and clicking **Message**.
- Newlines are typed as Shift+Enter only if P4-06 (#374) shows that Shift+Enter never sends, whatever the "Press Enter to Send" setting is. Otherwise lint refuses multi-line LinkedIn templates (P4-11, #377), and the prefill never presses any key. The capture shows that Shift+Enter never sends (see [Keys](#keys-shiftenter-only-never-enter)).
- The prefill releases the browser lock after typing. The handed-over tab stays open and is brought to the front. netkeeper never closes it.
- Auto-send (P4-04, #384) is built only after CP8 signs off, and needs its own amendment.

On 2026-10-05, the maintainer also accepted that the recipient may see "typing…" while the prefill types (see [The typing indicator](#the-typing-indicator)).

## What the capture showed

The 2026-10-05 capture (#374) settled these facts. They're structure only: no name, slug, URN, or message text.

| Fact | What the capture showed |
|---|---|
| The Message control | An `<a>` (role `link`) whose visible text is **Message**. Its `href` is `/messaging/compose/?profileUrn=…&recipient=…&screenContext=NON_SELF_PROFILE_VIEW&interop=msgOverlay`, so it names the recipient's profile. A sibling `<button aria-expanded>` holds the "more" menu. |
| What the click opens | A message bubble at the bottom of the profile page. The tab stays on the profile; it doesn't navigate. The click fetches `voyagerMessagingDashComposeOptions/<urn>` and `voyagerMessagingDashComposeViewContexts`. |
| The bubble, existing conversation | A `role="dialog"`. Its header `h2` links to the recipient's `/in/<slug>/`. |
| The bubble, never messaged | A new-message layout. The recipient shows as a chip with a **Remove <name>** button, beside a `role="combobox"` search field, and the chip links to the recipient's `/in/<slug>/`. |
| The composer | One `div[contenteditable][role="textbox"][aria-multiline="true"]` with `aria-label="Write a message…"`, inside `form#msg-form-…`. Both layouts use the same label. The label doesn't name the recipient. |
| Send | The form's `button[type="submit"]`. In the never-messaged layout, it stays disabled until the composer holds text. |
| Keys | Tested in both the bubble and `/messaging/`, with the same results. See [Keys](#keys-shiftenter-only-never-enter). |
| Typing | Typing fires `POST voyagerMessagingDashMessengerConversations?action=typing` (202, with `conversationUrn` in the body), several times for one word. The recipient probably sees "typing…". |
| Bubble persistence | A minimized bubble stays open across pages, and its draft survives. Closing the bubble deletes the draft. |
| Send request | `POST voyagerMessagingDashMessengerMessages?action=createMessage`. The prefill never sends; the inbox poll uses this shape to match a send the person made. |

The capture didn't show three things this ADR depends on. Each one fails safe as written here:

- **Whether the composer holds keyboard focus right after the Message click.** If it doesn't, the focus check refuses before the first key, and the prefill ends `not_typed`. This ADR authorizes no click into the composer and no `focus()` call. If the first supervised run shows that focus doesn't land there on its own, a new amendment names that input and its checks; P4-03 doesn't add it under this ADR.
- **Whether a desktop draft appears in the phone app** (#429). It doesn't change any check here.
- **Which field of the click's compose request names the recipient.** The summary shows the request (`voyagerMessagingDashComposeOptions/<urn>`) but not whether its URN is the recipient's profile URN. P4-03 takes that from the shape doc. If no request the click loads names the contact's profile URN, this ADR is revised before P4-03 merges (see [The recipient](#the-recipient)).

## Decision

netkeeper may give a LinkedIn page exactly two new inputs, both in `BrowserRun` (`netkeeper/linkedin/browser.py`), and one new way to end a run. Everything else in ADR 0006 stays: no script in the page, no request of netkeeper's own, and no interception. The run also brings its tab to the front once, at the start (see below).

### Who triggers it

A prefill runs only when a person asks for it, from the "ready to prefill" queue, while watching Chrome. P4-09's (#379) `claim_prefill` writes the message `scheduled`, records a manual `message_send` run, and submits it; `create_run` refuses a scheduled `message_send` run.

A prefill run never waits for the browser lock: it asks for it with `wait=False`, and if another run holds it, the prefill doesn't start. A prefill run is never retried and never resumed. A claim whose run hasn't started within N seconds lapses: the runner records `not_typed` ("the claim lapsed") before it spends any budget or navigates, and P4-09 gives the claim back to the queue. So a prefill never starts long after the person who asked has stopped watching. **N is a maintainer decision** (suggested: 60). P4-09 has no lapse of its own, so P4-03's runner checks it against the claim's time. A lapse counts toward P4-09's `NOT_TYPED_PARK_AFTER` like any other `not_typed`.

### The typing plan comes first

The runner builds the whole plan with `pacing.typing_plan` (P4-10, #376) before it spends any budget or navigates, as `typing_plan`'s docstring requires. A plan the module refuses costs nothing and types nothing:

- `TypingTooLong` is `too_long`.
- Every other `TypingPlanError` (`MultilineRefused`, `UnsupportedCharacter`, `TypingPlanMismatch`, `InvalidTypingProfile`) is `not_typed`. Lint and the fire path's rendered-message check (#382's comments) should stop these bodies first; this is the backstop.
- Any other exception from `typing_plan` is wrapped in a distinct `TypingPlanError` subclass (for example, `PlanInvariantBroken`). The wrapper catches `Exception` only, raises with a fixed message that never quotes the text, and chains the cause with `from`. The prefill handles it as `not_typed`, never retries, and logs only the exception's class. No lint rule matches it, so the lint and plan agreement fuzz can assert that it never fires (#426's review).

### Bringing the tab to the front

The run brings its tab to the front once, at the start, before `click_message`, while the person is watching. That's the only `bring_to_front` call in the package. Nothing later in the run changes focus, and `hand_over()` doesn't either: a focus change while the person is reading or typing elsewhere could send their keys somewhere they didn't intend.

This adjusts the maintainer's "brought to the front" wording, which placed it at the hand-over. The maintainer confirms it at acceptance.

### One click on Message

`BrowserRun.click_message` clicks the **Message** control once per prefill, on the contact's profile, modeled on `BrowserRun.click_contact_info`:

- It refuses when the run's tab is gone, and when the tab isn't on the contact's profile path (`/in/<slug>/`).
- It pauses the way a person does before reaching for the control.
- It finds the control by its accessible role and name: role `link`, name `Message`, exact. The role and name are module constants, pinned to literals.
- It refuses unless exactly one visible candidate names this contact: its `href` path is `/messaging/compose/`, and its `profileUrn` (or `recipient`) query parameter, URL-decoded, equals the contact's `urn:li:fsd_profile:<id>`. A candidate that names another profile, such as a "People also viewed" card's, is never clicked. If two visible candidates name the contact, it refuses. The capture didn't record the sticky header that appears on scroll, so the click happens with the page at the top, before the run scrolls down or after it has scrolled back.
- It clicks once, at the control's own box, with a person's press length. A click that fails isn't tried again.

This binds the click to the contact by URN, not by where the control sits. The draft's earlier rule (look only inside the top card, found by landmark) is replaced: the capture didn't show a landmark for the top card, and the `href` names the profile directly.

The click opens a bubble on the profile page, and the tab doesn't navigate. Any change of the tab's URL after the click stops the run.

### No reattach after the click

Once `click_message` returns, the run never calls `ensure_page` or `goto` and never reattaches. A lost tab or browser ends the run: `not_typed` before the first key, `unknown` after it. A reopened tab would be a new page with no verified composer, and a reattach could land keys in a tab nobody checked.

### Typing into one verified composer

`BrowserRun.type_into_composer` types the rendered body into the composer the click opened. Before the first key, it verifies all of these, and types nothing if any fails:

- **Exactly one composer is on the page.** The composer is found by role and name: role `textbox`, name `Write a message…` (the label as the shape doc records it, with its ellipsis character), exact, never by a CSS class or the `msg-form-…` id. It must sit inside a message bubble. Any other composer on the page, such as a minimized bubble left from an earlier prefill or opened by the person, means more than one, and the prefill refuses with a reason that asks the person to close the other message bubbles. netkeeper never closes a bubble: closing one deletes its draft, and it's an input this ADR doesn't authorize.
- **The composer is empty.** A composer that holds any text is refused. The capture shows that a minimized bubble keeps its draft across pages, so the Message click can restore a bubble that already holds a draft for this contact. The prefill refuses, and the person clears the draft.
- **The composer's recipient is this contact.** See the next subsection.
- **The verified composer holds focus.** Focus is read through Playwright's own selector engine (a `:focus` match on the composer's locator), never through `evaluate` or other script of netkeeper's in the page. If the composer doesn't hold focus, the prefill types nothing.
- **No chunk holds a control character.** The method refuses, before the first key, a plan in which any chunk contains `\n`, `\r`, or any other C0 or C1 control character. Playwright's `keyboard.type` maps `\n` and `\r` to the Enter key, so a newline is only ever a `newline=True` step of the plan, pressed as Shift+Enter. `TypeStep`'s constructor already refuses such a chunk; this check doesn't rely on it.

#### The recipient

The composer's `aria-label` doesn't name the recipient, so the recipient comes from the bubble and from what the page loaded. All of these must pass:

1. **The Message control** named the contact's profile URN (checked in `click_message`).
2. **The bubble** names the contact's public id:
   - in an existing conversation, the bubble's header `h2` links to `/in/<slug>/`, and the slug equals the contact's public id;
   - in the never-messaged layout, there's exactly one recipient chip (exactly one **Remove …** button beside the combobox), and its link is `/in/<slug>/` for the contact's public id. Zero chips, two chips, or a chip for anyone else is refused.

   A contact without a public id can't be checked this way, and the prefill ends `not_typed`.
3. **What the page loaded** names the contact's profile URN, read through `BrowserRun.observe` (ADR 0006) from the answer the click itself caused: the compose request (`voyagerMessagingDashComposeOptions/<urn>`), at the field the shape doc names. In an existing conversation, the conversation's URN, when the page loaded it, goes on the outcome (`MessageOutcome.conversation_urn`); a participant list that names someone else is refused.

The bubble's DOM alone never authorizes typing. The draft's earlier rule ("no observed conversation means `not_typed`") is replaced: a contact you've never messaged has no conversation, and the capture shows how that bubble names its recipient.

#### The replay

Typing replays the plan: lognormal delays, median 140 ms a character, no typos, a 300-second ceiling. A step whose chunk is a single printable ASCII character is typed with `keyboard.type`; any other chunk (`TypeStep.needs_insert_text`: an accented letter, an emoji sequence) is sent with `keyboard.insert_text`. A `newline=True` step is pressed as Shift+Enter. The run checks for cancel between steps, and stops when the tab closes or its URL changes.

### Checked before every chunk

The checks above don't run only once. Playwright's `keyboard.type` sends keys to whatever has focus and doesn't check where that is, and it maps a space to the Space key. If focus moved mid-type, to the Send button for example, a Space or a Shift+Enter could send the message, and the rest of the body could land in the wrong place. In the never-messaged layout, focus could also move to the combobox, and the rest of the body would become a people search.

- Before every chunk and every Shift+Enter, `type_into_composer` checks again that exactly one composer is on the page, that the bubble's recipient (the header link, or the one chip) is unchanged, that the verified composer holds focus, and that its text equals the prefix typed so far.
- Any failed check stops typing at once, with the outcome `partially_typed`.
- Before the hand-over, the composer's text must equal the body, or the outcome is `partially_typed`.
- The prefill UI tells the person not to type or click in Chrome while the prefill types.

### The typing indicator

Typing fires the page's own `action=typing` requests, several times a word, so the recipient probably sees "typing…" for as long as the prefill types (up to the plan's 300-second ceiling), even if the person never sends. These are the page's own requests, which ADR 0006 allows; netkeeper sends nothing of its own.

The maintainer accepted this on 2026-10-05. The **Prefill** button's copy tells the person that the recipient may see "typing…". A setting to suppress the indicator is a possible later addition (#430), low priority and not part of the initial release; it would need its own amendment, because any way to suppress it is an input or a request this ADR doesn't authorize.

### Keys: Shift+Enter only, never Enter

The capture ran this table in the bubble the Message click opens and on `/messaging/`, with the same results. The default setting is **Click Send** (Press Enter to Send off).

| Setting | Key | New line or sent? |
|---|---|---|
| On | Shift+Enter | new line |
| On | Enter | sent |
| Off | Shift+Enter | new line |
| Off | Enter | new line |
| Off | ⌘+Enter | sent |

Shift+Enter never sends, whatever the setting. So:

- The prefill types each newline as Shift+Enter. That's the only key it ever `press`es.
- It never presses Enter, NumpadEnter, Ctrl+Enter, or ⌘+Enter (Meta+Enter), and never holds a modifier down. Enter sends with the setting on; ⌘+Enter sends with it off.
- It never clicks Send, and never locates the form's `button[type="submit"]`.

**Multi-line templates.** P4-11 (#387) lints a LinkedIn body's newlines (CR, LF, or CRLF) as an error while `pacing.SHIFT_ENTER_NEWLINES_ALLOWED` is false, and `typing_plan` refuses them under the same flag. That flag is the single source of truth. P4-03 sets it to true in the same pull request that adds the Shift+Enter press and its pins, and multi-line LinkedIn templates then lint clean. Every other line break (VT, FF, NEL, U+2028, U+2029, and the rest of `LINE_BREAK_CHARS`) stays refused, whatever the flag says. (#382 calls the flag `LINKEDIN_ALLOW_NEWLINES`; the name #387 shipped is `SHIFT_ENTER_NEWLINES_ALLOWED`.)

The Send click isn't authorized here. Auto-send (P4-04, #384) needs its own amendment, gated on `[campaigns] linkedin_auto_send` and the step's `auto_send` mode, and is built only after CP8 signs off.

### Handing the tab over

At the end of a prefill run (spec 11.6), `BrowserRun.hand_over()` drops the run's reference to its tab and detaches without closing it, so the provider's later `close()` closes nothing. It changes no focus (see [Bringing the tab to the front](#bringing-the-tab-to-the-front)). From then on the tab isn't netkeeper's: no later run reuses it, navigates it, or closes it. `hand_over` is a new ending, distinct from `close()`, and is reached only from `netkeeper/linkedin/page_messaging.py`. The run releases the browser lock after typing; it doesn't wait for the send.

The bubble stays open in the handed-over tab, and the capture shows it keeps its draft if the person minimizes it or moves to another page. Because the next prefill refuses a page with more than one composer, the person closes that bubble (after sending, or to discard the draft) before the next prefill. P4-09 allows one open prefill at a time.

**Confirming the send.** The next inbox poll (P4-02, #381), or the "I sent it, check now" action, confirms the send by matching the first outbound message in the conversation dated after `prefilled_at`. So `prefilled_at` must be a time before the first key: the moment typing starts, taken before the first keystroke (#416's review, #382's comments). P4-09's `record_prefill_outcome` sets `prefilled_at` to the `now` it's given when it records `prefilled`; P4-03 passes the typing start, not the time it records the outcome, or adds an argument for it. If `prefilled_at` were later than the person's click on Send, or the clocks disagreed by a few seconds, the send would never be confirmed.

Every outcome after the first key hands the tab over, not only `prefilled`. After a failure mid-type, the tab still holds what was typed:

- The UI flags `partially_typed` and `unknown` with "Part of a message is in the composer and may be kept as a draft. Clear it, or close the message bubble, which deletes the draft."
- netkeeper never clears it. Clearing is an input this ADR doesn't authorize.
- If the run crashes, the message stays claimed, and P4-09 lists it as interrupted under "waiting for you", holding the one open slot until the person discards it. It's never claimed or typed again.

### The pins

P4-03 (#382) changes `tests/test_browser_safety.py` in the same pull request as the code these pins guard, not in this one.

Static pins:

- `ALLOWED_INPUTS` entries for exactly two methods: `click` in `BrowserRun.click_message`; and `keyboard` (read once, into a local), `type`, `insert_text`, and `press` in `BrowserRun.type_into_composer`.
- A literal check that the only `press` argument anywhere is `"Shift+Enter"`.
- No `keyboard.down`, no `keyboard.up`, and no `focus()` anywhere.
- The Message control's role and name (`"link"`, `"Message"`) and the composer's role and name (`"textbox"`, `"Write a message…"`) are module constants pinned to literals. No role, name, or selector literal under `netkeeper/linkedin/` matches "send" or "submit", ignoring case.
- `bring_to_front` is called at one site, inside the prefill's start, before `click_message`; a runtime pin checks that it's called once per run.
- `netkeeper/linkedin/page_messaging.py` in `BROWSER_MODULES` and `BROWSER_CALLERS`.
- `hand_over` reachable only from `page_messaging.py`.
- `pacing.SHIFT_ENTER_NEWLINES_ALLOWED` is true only together with the Shift+Enter press pin.

Runtime pins, against a fake page:

- A plan with a `"\n"` chunk gives `not_typed` with zero keys.
- A `typing_plan` failure, including an unexpected exception, gives `too_long` or `not_typed` with zero keys, no navigation, and no budget spent.
- A second visible Message control that names the contact, or none that does, gives `not_typed` with no click.
- A second composer on the page before the first key gives `not_typed`; one appearing mid-type stops typing.
- A non-empty composer gives `not_typed`.
- A never-messaged bubble with zero chips, two chips, or a chip for another profile gives `not_typed`.
- Focus not in the composer before the first key gives `not_typed`; focus moving away after chunk k gives zero further keys.
- Composer text that diverges from the typed prefix stops typing.
- A recipient (header link or chip) that changes mid-type stops typing.
- A final composer text that differs from the body gives `partially_typed`, never `prefilled`.
- A `prefilled` outcome records a `prefilled_at` no later than the first key.
- After `hand_over()`, the provider's exit leaves the page open.

P4-03's loopback replica (`tests/smoke/test_prefill_smoke.py`) copies the capture's layout: the Message link with its compose `href`, both bubble layouts, the composer, a submit button, and an Enter-to-send toggle that records a "sent" event on a bare Enter when it's on and on ⌘+Enter when it's off.

## Consequences

- netkeeper can type a message into LinkedIn, and no key or click of netkeeper's is meant to send one. What the user sees in the handed-over tab is the rendered body, in the right conversation, waiting for their click.
- Multi-line LinkedIn templates become possible once P4-03 lands, typed with Shift+Enter, which the capture shows never sends.
- The recipient probably sees "typing…" while the prefill types, and for up to 300 seconds even if the person never sends. The maintainer accepted this for the initial release (#430 tracks a possible toggle).
- Every refusal before the first key leaves nothing typed (`not_typed`), so a refused prefill costs at most a budget unit, not a wrong message. A prefill interrupted after the first key (`partially_typed`, `unknown`) is never retried: retyping into a composer that may hold half the message is worse than a person finishing it. The person clears what's there.
- Each prefill spends one `li_prefills` unit and one `profile_visits` unit before it navigates (P4-03, P4-09).
- A prefill that can't get the browser lock at once doesn't start, and a claim that waits too long lapses. A person may have to click **Prefill** again.
- Any other message bubble on the page stops the prefill before the first key. The person closes earlier bubbles, including the last prefill's, before the next one.
- Focus is checked before each chunk, not atomically with it. If focus moves in the moment between the check and the key, one chunk can land outside the composer. The per-chunk checks stop the prefill on the next chunk. This window is the remaining risk, and the reason the person watches and leaves Chrome alone.
- If focus doesn't land in the composer after the Message click, every prefill ends `not_typed` until an amendment authorizes a click into the composer.
- Handed-over tabs accumulate in the user's Chrome until the user closes them. That's deliberate: netkeeper closing a tab is how a draft would be lost.
- The person must leave Chrome alone while the prefill types. Touching it stops the prefill as `partially_typed`, which is the safe direction.
- If LinkedIn changes the Message control, the composer, or how the bubble shows its recipient, the prefill refuses and types nothing. That's the intended failure.
- If LinkedIn changes what Shift+Enter does, nothing in this ADR detects it at run time. The capture is the evidence, and a later capture that contradicts it means newlines are refused (the flag goes back to false) until a new amendment.
- A future contributor keeps these true: the only clicks are Contact info and Message, each once, each bound to the profile it's on; the only typing is aimed at one verified composer for the contact, with focus and the composer checked again before every chunk; the only key pressed is Shift+Enter; no code path presses Enter or clicks Send; and a handed-over tab is never touched again.

## Decisions the maintainer makes at acceptance

1. **Where the tab comes to the front.** This ADR brings it to the front once, at the run's start, and `hand_over()` changes no focus. That adjusts the "brought to the front" wording of 2026-10-03.
2. **N, the claim lapse.** How many seconds a claim may wait to start before it lapses back to the queue (suggested: 60).
3. **Other open bubbles.** This ADR refuses a page that shows any composer besides the one the click opened, so the person closes earlier bubbles, including the last prefill's, before the next prefill. The alternative, allowing minimized bubbles for other people and relying on the focus and recipient checks, is looser.
4. **Binding the Message click by `href`, not by the top card.** This ADR clicks the one visible Message link whose `href` names the contact's profile URN, wherever it sits, instead of the draft's top-card rule.

The typing indicator, a decision in the draft, was settled on 2026-10-05: accepted, with a possible toggle later (#430).
