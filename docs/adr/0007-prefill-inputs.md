# 0007. The prefill's inputs: one Message click, typing into one verified composer, never Enter

Date: 2026-10-03, updated 2026-10-05 with the P4-06 messaging capture (#374)

## Status

Proposed

The maintainer accepts this ADR in review. Before acceptance, the maintainer makes the [decisions listed for acceptance](#decisions-the-maintainer-makes-at-acceptance), and P4-06's analysis (`docs/linkedin-messaging-shapes.md`, #374) merges first. The facts below come from that analysis (PR #432), which corrects the first summary on #374 in several places: the profile renders three Message controls, the existing conversation's header links by profile id rather than by slug, and the compose requests name the recipient.

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
| The Message control | An `<a>` (role `link`) with no `aria-label`, whose text and accessible name are **Message**. Its `href` is `/messaging/compose/?profileUrn=urn:li:fsd_profile:<id>&recipient=<id>&screenContext=NON_SELF_PROFILE_VIEW&interop=msgOverlay`, with `recipient` the same bare id as in `profileUrn`. The server-rendered profile writes it relative; the live page had it absolute. A sibling `<button aria-expanded>` named **More** holds the overflow menu. |
| How many | **Three** `<a>` elements named exactly **Message**, each with its own `componentkey`, all with the same compose `href` for the profile's own id. Which one a person sees (the top card, the sticky header that appears on scroll, or a hidden layout variant) is CSS, which the capture can't show. |
| What the click opens | A message bubble at the bottom of the profile page. The tab stays on the profile. The click loads the compose option, `GET voyagerMessagingDashComposeOptions/urn:li:fsd_composeOption:(<recipient's bare profile id>,NON_SELF_PROFILE_VIEW,<token>)`, and the view context (`voyagerMessagingDashComposeViewContexts`). |
| The compose option's answer | `data.composeNavigationContext.recipientUrns[0]` is the recipient's `urn:li:fsd_profile:<id>`, matching the path's id. An existing conversation has `composeOptionType` `REPLY` and `existingConversationUrn` (`urn:li:fsd_conversation:<thread id>`); a never-messaged contact has `CONNECTION_MESSAGE` and no `existingConversationUrn`. The view context's answer names no recipient. |
| The bubble, existing conversation | A `div` with `role="dialog"` and `aria-label="Messaging"`. Its header `h2` has one link, `/in/<profile id>/`: by **profile id, not vanity slug**. |
| The bubble, never messaged | A `New message` heading, a recipient field labelled `Enter message recipients` (`input[role="combobox"]`), and the recipient as a chip: a `button` named `Remove <name>`. A card below links to the recipient by **vanity slug**, `/in/<slug>/`. Whether the bubble's root is `role="dialog"` is **not known**: the copied HTML starts below it. |
| The composer | One `div[contenteditable="true"][role="textbox"][aria-multiline="true"]` named `Write a message…` (U+2026), inside `form#msg-form-…`. Both layouts use the same label, and it doesn't name the recipient. |
| Send | The form's only `button[type="submit"]`, text `Send`. In the never-messaged layout it's disabled until the composer holds text. A `button` named `Open send options` beside it holds the "Press Enter to Send" setting. |
| Keys | Tested in both the bubble and `/messaging/`, with the same results. See [Keys](#keys-shiftenter-only-never-enter). |
| Typing | The page posts `voyagerMessagingDashMessengerConversations?action=typing` (202, body `{"conversationUrn": …}`), throttled to about one post every 5 seconds while typing goes on. No draft save was seen. The recipient probably sees "typing…". |
| Bubble persistence | A minimized bubble stays open across pages, and its draft survives. Closing the bubble deletes the draft. |
| Send request | `POST voyagerMessagingDashMessengerMessages?action=createMessage`. The prefill never sends; the inbox poll uses the list and thread answers to match a send the person made. |

The capture didn't show three things this ADR depends on. Each one fails safe as written here:

- **Whether the composer holds keyboard focus right after the Message click** (#429, item 5). If it doesn't, the focus check refuses before the first key, and the prefill ends `not_typed`. The shape doc suggests that P4-03 focus the composer itself; this ADR doesn't authorize that. It authorizes no click into the composer and no `focus()` call, so P4-03 doesn't add one. If the first supervised run shows that focus doesn't land there on its own, a new amendment names that input and its checks (see [the decisions](#decisions-the-maintainer-makes-at-acceptance)).
- **Which Message control is visible** (#429, item 6). The click rule below doesn't depend on it.
- **Whether the never-messaged bubble's root is `role="dialog"`.** The checks for that layout don't rely on it.

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
- It finds the controls by their accessible role and name: role `link`, name `Message`, exact. The role and name are module constants, pinned to literals.
- It refuses unless every control named **Message** on the page, hidden or visible, names this contact. Its `href` path must be `/messaging/compose/`, relative or on `www.linkedin.com`, and both its `profileUrn` (URL-decoded) and its `recipient` must be the contact's: `urn:li:fsd_profile:<id>` and `<id>`. Zero controls, or any control that names another profile or holds no such `href`, means no click. The capture rendered three identical controls, so a rule of "exactly one control" would refuse every profile; this rule accepts any number of controls, but only when they all lead to the same compose for the same person.
- It clicks exactly one of them: the first visible one in document order. Because every candidate carries the same `href`, which one it picks doesn't change who the bubble is for. If none is visible, it refuses.
- It clicks once, at the control's own box, with a person's press length. A click that fails isn't tried again.

This binds the click to the contact by URN, not by where the control sits or how many there are. The draft's earlier rules (look only inside the top card; refuse more than one control) are replaced: the capture showed no landmark for the top card, and three controls on every profile.

The click opens a bubble on the profile page, and the tab doesn't navigate. Any change of the tab's URL after the click stops the run.

### No reattach after the click

Once `click_message` returns, the run never calls `ensure_page` or `goto` and never reattaches. A lost tab or browser ends the run: `not_typed` before the first key, `unknown` after it. A reopened tab would be a new page with no verified composer, and a reattach could land keys in a tab nobody checked.

### Typing into one verified composer

`BrowserRun.type_into_composer` types the rendered body into the composer the click opened. Before the first key, it verifies all of these, and types nothing if any fails:

- **Exactly one composer is on the page.** The composer is found by role and name: role `textbox`, name `Write a message…` (with U+2026, the ellipsis character), exact, never by a CSS class or the `msg-form-…` id. It must sit in the bubble the recipient checks verified (see the next subsection). Any other composer on the page, such as a minimized bubble left from an earlier prefill or opened by the person, means more than one, and the prefill refuses with a reason that asks the person to close the other message bubbles. netkeeper never closes a bubble: closing one deletes its draft, and it's an input this ADR doesn't authorize.
- **The composer is empty.** A composer that holds any text is refused. The capture shows that a minimized bubble keeps its draft across pages, so the Message click can restore a bubble that already holds a draft for this contact. The prefill refuses, and the person clears the draft.
- **The composer's recipient is this contact.** See the next subsection.
- **The verified composer holds focus.** Focus is read through Playwright's own selector engine (a `:focus` match on the composer's locator), never through `evaluate` or other script of netkeeper's in the page. If the composer doesn't hold focus, the prefill types nothing.
- **No chunk holds a control character.** The method refuses, before the first key, a plan in which any chunk contains `\n`, `\r`, or any other C0 or C1 control character. Playwright's `keyboard.type` maps `\n` and `\r` to the Enter key, so a newline is only ever a `newline=True` step of the plan, pressed as Shift+Enter. `TypeStep`'s constructor already refuses such a chunk; this check doesn't rely on it.

#### The recipient

The composer's `aria-label` doesn't name the recipient, so the recipient comes from the Message control, from what the page loaded, and from the bubble. All three must pass:

1. **The Message control.** Every control named **Message** named the contact's profile URN and bare id (checked in `click_message`).
2. **What the page loaded.** Through `BrowserRun.observe` (ADR 0006), the run reads the compose option the click caused, `GET voyagerMessagingDashComposeOptions/<fsd_composeOption urn>`:
   - the first part of the `fsd_composeOption` URN in the request's path is the contact's bare profile id;
   - the answer's `data.composeNavigationContext.recipientUrns` is exactly one URN, the contact's `urn:li:fsd_profile:<id>`;
   - `composeOptionType` is `REPLY` with an `existingConversationUrn`, or `CONNECTION_MESSAGE` without one. Any other type, a `REPLY` without a conversation, or a `CONNECTION_MESSAGE` with one is refused.

   No compose option seen, or more than one, means `not_typed`. The type decides which bubble layout the next check expects.
3. **The bubble.** The run checks the layout the compose option named, and refuses if the page shows the other one:
   - **`REPLY`, an existing conversation.** Exactly one `role="dialog"` named `Messaging` holds the composer, and its header `h2` links to `/in/<profile id>/`, where the id is the contact's bare profile id (the same id as in the URN, not the vanity slug).
   - **`CONNECTION_MESSAGE`, never messaged.** This check doesn't rely on `role="dialog"`, which the capture didn't confirm. Instead, page-wide: exactly one `New message` heading, exactly one recipient field named `Enter message recipients`, exactly one chip (a button whose name starts `Remove `), and the card beside it links to `/in/<slug>/` for the contact's public id. Zero chips, two chips, or a card for anyone else is refused. A contact without a public id can't be checked this way, and the prefill ends `not_typed`. The chip's name isn't compared with the contact's stored name, which the person may have edited; the URN checks and the slug bind the recipient.

The bubble's DOM alone never authorizes typing, and neither does the compose answer alone. The draft's earlier rule ("no observed conversation means `not_typed`") is replaced: a contact you've never messaged has no conversation, and the compose option names the recipient either way.

For an existing conversation, the outcome's `conversation_urn` comes from `existingConversationUrn`. Its thread id is the same one the page's following `messengerMessages` request names, so P4-03 reports it in the form the inbox poll (P4-01) matches.

#### The replay

Typing replays the plan: lognormal delays, median 140 ms a character, no typos, a 300-second ceiling. A step whose chunk is a single printable ASCII character is typed with `keyboard.type`; any other chunk (`TypeStep.needs_insert_text`: an accented letter, an emoji sequence) is sent with `keyboard.insert_text`. A `newline=True` step is pressed as Shift+Enter. The run checks for cancel between steps, and stops when the tab closes or its URL changes.

### Checked before every chunk

The checks above don't run only once. Playwright's `keyboard.type` sends keys to whatever has focus and doesn't check where that is, and it maps a space to the Space key. If focus moved mid-type, to the Send button for example, a Space or a Shift+Enter could send the message, and the rest of the body could land in the wrong place. In the never-messaged layout, focus could also move to the combobox, and the rest of the body would become a people search.

- Before every chunk and every Shift+Enter, `type_into_composer` checks again that exactly one composer is on the page, that the bubble's recipient is unchanged (the dialog's header link, or the one chip and its card's link), that the verified composer holds focus, and that its text equals the prefix typed so far.
- Any failed check stops typing at once, with the outcome `partially_typed`.
- Before the hand-over, the composer's text must equal the body, or the outcome is `partially_typed`.
- The prefill UI tells the person not to type or click in Chrome while the prefill types.

### The typing indicator

Typing fires the page's own `action=typing` posts, throttled to about one every 5 seconds while typing goes on, so the recipient probably sees "typing…" for as long as the prefill types (up to the plan's 300-second ceiling), even if the person never sends. These are the page's own requests, which ADR 0006 allows; netkeeper sends nothing of its own.

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
- The Message control's role and name (`"link"`, `"Message"`), the composer's role and name (`"textbox"`, `"Write a message…"`), the dialog's name (`"Messaging"`), the `"New message"` heading, and the `"Enter message recipients"` field are module constants pinned to literals. No role, name, or selector literal under `netkeeper/linkedin/` matches "send" or "submit", ignoring case.
- `bring_to_front` is called at one site, inside the prefill's start, before `click_message`; a runtime pin checks that it's called once per run.
- `netkeeper/linkedin/page_messaging.py` in `BROWSER_MODULES` and `BROWSER_CALLERS`.
- `hand_over` reachable only from `page_messaging.py`.
- `pacing.SHIFT_ENTER_NEWLINES_ALLOWED` is true only together with the Shift+Enter press pin.

Runtime pins, against a fake page:

- A plan with a `"\n"` chunk gives `not_typed` with zero keys.
- A `typing_plan` failure, including an unexpected exception, gives `too_long` or `not_typed` with zero keys, no navigation, and no budget spent.
- Three Message controls with the contact's `href` give one click, on the first visible one.
- Any Message control whose `href` names another profile, a mismatched `profileUrn` and `recipient`, no control, or no visible control gives `not_typed` with no click.
- A compose option whose path id or `recipientUrns` isn't the contact's, with more than one recipient, with an unknown `composeOptionType`, or missing, gives `not_typed`.
- A `REPLY` bubble whose header links to another profile id, or a page showing the layout the compose option didn't name, gives `not_typed`.
- A second composer on the page before the first key gives `not_typed`; one appearing mid-type stops typing.
- A non-empty composer gives `not_typed`.
- A never-messaged bubble with zero chips, two chips, two `New message` headings, or a card for another slug gives `not_typed`, with or without a `role="dialog"` root.
- Focus not in the composer before the first key gives `not_typed`; focus moving away after chunk k gives zero further keys.
- Composer text that diverges from the typed prefix stops typing.
- A recipient (header link or chip) that changes mid-type stops typing.
- A final composer text that differs from the body gives `partially_typed`, never `prefilled`.
- A `prefilled` outcome records a `prefilled_at` no later than the first key.
- After `hand_over()`, the provider's exit leaves the page open.

P4-03's loopback replica (`tests/smoke/test_prefill_smoke.py`) copies the capture's layout: three Message links with the same compose `href` (one visible), a compose option answer, both bubble layouts (the never-messaged one without a `role="dialog"` root), the composer, a submit button, and an Enter-to-send toggle that records a "sent" event on a bare Enter when it's on and on ⌘+Enter when it's off.

## Consequences

- netkeeper can type a message into LinkedIn, and no key or click of netkeeper's is meant to send one. What the user sees in the handed-over tab is the rendered body, in the right conversation, waiting for their click.
- Multi-line LinkedIn templates become possible once P4-03 lands, typed with Shift+Enter, which the capture shows never sends.
- The recipient probably sees "typing…" while the prefill types, and for up to 300 seconds even if the person never sends. The maintainer accepted this for the initial release (#430 tracks a possible toggle).
- Every refusal before the first key leaves nothing typed (`not_typed`), so a refused prefill costs at most a budget unit, not a wrong message. A prefill interrupted after the first key (`partially_typed`, `unknown`) is never retried: retyping into a composer that may hold half the message is worse than a person finishing it. The person clears what's there.
- Each prefill spends one `li_prefills` unit and one `profile_visits` unit before it navigates (P4-03, P4-09).
- A prefill that can't get the browser lock at once doesn't start, and a claim that waits too long lapses. A person may have to click **Prefill** again.
- Any other message bubble on the page stops the prefill before the first key. The person closes earlier bubbles, including the last prefill's, before the next one.
- Focus is checked before each chunk, not atomically with it. If focus moves in the moment between the check and the key, one chunk can land outside the composer. The per-chunk checks stop the prefill on the next chunk. This window is the remaining risk, and the reason the person watches and leaves Chrome alone.
- If focus doesn't land in the composer after the Message click, every prefill ends `not_typed` until an amendment authorizes focusing it. The shape doc expects P4-03 to need that; until #429's check, this ADR doesn't assume it.
- Handed-over tabs accumulate in the user's Chrome until the user closes them. That's deliberate: netkeeper closing a tab is how a draft would be lost.
- The person must leave Chrome alone while the prefill types. Touching it stops the prefill as `partially_typed`, which is the safe direction.
- If LinkedIn changes the Message control, the compose option, the composer, or how the bubble shows its recipient, the prefill refuses and types nothing. That's the intended failure.
- If LinkedIn changes what Shift+Enter does, nothing in this ADR detects it at run time. The capture is the evidence, and a later capture that contradicts it means newlines are refused (the flag goes back to false) until a new amendment.
- A future contributor keeps these true: the only clicks are Contact info and Message, each once, each bound to the profile it's on; the only typing is aimed at one verified composer for the contact, with focus and the composer checked again before every chunk; the only key pressed is Shift+Enter; no code path presses Enter or clicks Send; and a handed-over tab is never touched again.

## Decisions the maintainer makes at acceptance

1. **Where the tab comes to the front.** This ADR brings it to the front once, at the run's start, and `hand_over()` changes no focus. That adjusts the "brought to the front" wording of 2026-10-03.
2. **N, the claim lapse.** How many seconds a claim may wait to start before it lapses back to the queue (suggested: 60).
3. **Other open bubbles.** This ADR refuses a page that shows any composer besides the one the click opened, so the person closes earlier bubbles, including the last prefill's, before the next prefill. The alternative, allowing minimized bubbles for other people and relying on the focus and recipient checks, is looser.
4. **The Message click rule.** The profile renders three identical Message controls. This ADR requires every one of them to name the contact's URN and bare id, then clicks the first visible one, once. It replaces the draft's "exactly one, in the top card" rule.
5. **Focusing the composer.** Where focus lands after the click is unknown (#429, item 5), and the shape doc expects P4-03 to focus the composer itself. This ADR doesn't authorize that, so if focus doesn't land there, every prefill ends `not_typed`. You can accept it as written and amend after #429's check, or authorize now one focusing input on the verified composer (a click on it, or `Locator.focus()`), pinned like the others and made only after every recipient check passes.

The typing indicator, a decision in the draft, was settled on 2026-10-05: accepted, with a possible toggle later (#430).
