# 0007. The prefill's inputs: one Message click, typing into one verified composer, never Enter

Date: 2026-10-03, updated 2026-10-05 with the P4-06 messaging capture (#374)

## Status

Proposed

The maintainer accepts this ADR in review. Before acceptance, the maintainer makes the [decisions listed for acceptance](#decisions-the-maintainer-makes-at-acceptance), and P4-06's analysis (`docs/linkedin-messaging-shapes.md`, #374) merges first. The facts below come from that analysis (PR #432), which corrects the first summary on #374 in several places: the profile renders three Message controls, the existing conversation's header links by profile id rather than by slug, and the compose requests name the recipient.

## Context

[ADR 0004](0004-manual-linkedin-sends-by-default.md) says the LinkedIn step's default mode is `prefill`: the sidecar opens the conversation in the user's Chrome, types the rendered message, and stops, and the user clicks Send. It doesn't say how that typing is made safe.

[ADR 0006](0006-observe-dont-request.md) says the only input netkeeper gives a LinkedIn page is navigation, the scroll replay (with its pointer rest, #192), and one click on **Contact info** per profile visit (#190). `tests/test_browser_safety.py` enforces that: `INPUT_CALLS` names every click, key, tap, hover, typing, focus, and synthetic event, and `ALLOWED_INPUTS` allows exactly one `click` (`BrowserRun.click_contact_info`) and one `move` (`BrowserRun._rest_pointer_over_content`) in the extractor. A prefill can't be built under those rules, and it shouldn't be built by loosening them case by case in a code review.

Three risks shape the exceptions:

- **A sent message can't be taken back.** A key that sends, or a click on Send, turns a prefill into an automated message: the thing ADR 0004 exists to prevent, and what LinkedIn restricts accounts for fastest. The capture shows that LinkedIn has a **Press Enter to Send** setting, off by default ("Click Send"), and that whether Enter sends depends on it. netkeeper doesn't control that setting. With the setting off, Command+Enter sends.
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
| Typing | The page posts `voyagerMessagingDashMessengerConversations?action=typing` (202, body `{"conversationUrn": …}`), throttled to about one post every 5 seconds while typing goes on. No draft-save request was seen. The recipient probably sees "typing…". |
| Bubble persistence | A minimized bubble stays open across pages, and its draft survives. Closing the bubble deletes the draft. |
| Send request | `POST voyagerMessagingDashMessengerMessages?action=createMessage`. The prefill never sends; the inbox poll uses the list and thread answers to match a send the person made. |

The capture didn't show four things this ADR depends on. Each one fails safe as written here:

- **Whether the composer holds keyboard focus right after the Message click** (#429, item 5). The shape doc expects P4-03 to focus the composer itself. Whether netkeeper may do that is [decision 5](#focusing-the-composer-decision-5). Under either answer, a composer without focus after the checks gets no key, and the prefill ends `not_typed`.
- **Which Message control is visible** (#429, item 6). The click rule below doesn't depend on it.
- **Whether a half-typed desktop draft appears in the phone app** (#429). No draft-save request appeared while typing, so the capture gives no sign that a draft leaves the browser; no check here depends on it.
- **Whether the never-messaged bubble's root is `role="dialog"`.** The checks for that layout don't rely on it.

## Decision

netkeeper may give a LinkedIn page exactly two new inputs, both in `BrowserRun` (`netkeeper/linkedin/browser.py`), and one new way to end a run. If the maintainer chooses option B of [decision 5](#focusing-the-composer-decision-5), a third input, one `Locator.focus()` on the verified composer, is added. Everything else in ADR 0006 stays: no script in the page, no request of netkeeper's own, and no interception. The run also brings its tab to the front once, at the start (see below).

### Who triggers it

A prefill runs only when a person asks for it, from the "ready to prefill" queue, while watching Chrome. P4-09's (#379) `claim_prefill` writes the message `scheduled`, stamps its `Message.scheduled_at` with the claim's time, records a manual `message_send` run, and submits it; `create_run` refuses a scheduled `message_send` run.

A prefill run is never retried and never resumed. Before it spends any budget or navigates, its runner settles two things, and each ends the run as `not_typed`, so P4-09 gives the claim back and the message doesn't stay interrupted, holding the one open slot:

- **The browser lock.** The run asks for it with `wait=False`. If another run holds it, the run records `not_typed` ("the browser was busy").
- **The claim lapse.** If more than N seconds have passed since `Message.scheduled_at`, the run records `not_typed` ("the claim lapsed"). So a prefill never starts long after the person who asked has stopped watching. **N is a maintainer decision** (suggested: 60). P4-09 has no lapse of its own.

Both count toward P4-09's `NOT_TYPED_PARK_AFTER` like any other `not_typed`.

### The typing plan comes first

The runner builds the whole plan with `pacing.typing_plan` (P4-10, #376) before it spends any budget or navigates, as `typing_plan`'s docstring requires. A plan the module refuses costs nothing and types nothing:

- `TypingTooLong` is `too_long`.
- Every other `TypingPlanError` (`MultilineRefused`, `UnsupportedCharacter`, `TypingPlanMismatch`, `InvalidTypingProfile`) is `not_typed`. Lint and the fire path's rendered-message check (#382's comments) should stop these bodies first; this is the backstop.
- Any other exception from `typing_plan` is wrapped in a distinct `TypingPlanError` subclass (for example, `PlanInvariantBroken`). The wrapper catches `Exception` only, raises with a fixed message that never quotes the text, and chains the cause with `from`. The prefill handles it as `not_typed`, never retries, and logs only the exception's class. No lint rule matches it, so the lint and plan agreement fuzz can assert that it never fires (#426's review).

The runner calls `typing_plan` without `allow_newlines=`, so the flag's default, `pacing.SHIFT_ENTER_NEWLINES_ALLOWED`, decides. That default is bound when the function is defined, so the module constant in source is the only switch: no code under `netkeeper/` passes `allow_newlines=` (a pin below), and a test that patches the constant at run time doesn't change what `typing_plan` does.

### Bringing the tab to the front

The run brings its tab to the front once, at the start, before `click_message`, while the person is watching. That's the only `bring_to_front` call in the package. Nothing later in the run changes which tab or window is in front, and `hand_over()` doesn't either: a change while the person is reading or typing elsewhere could send their keys somewhere they didn't intend.

This adjusts the maintainer's "brought to the front" wording, which placed it at the hand-over. The maintainer confirms it at acceptance.

### One click on Message

Before the click, the run reads the profile page's `h1`, if there's exactly one, for the chip check below. It also opens the observation (`BrowserRun.observe`, ADR 0006) for the compose option the click will load, so the answer can't arrive before anyone listens.

`BrowserRun.click_message` clicks the **Message** control once per prefill, on the contact's profile, modeled on `BrowserRun.click_contact_info`:

- It refuses when the run's tab is gone, and when the tab isn't on the contact's profile path (`/in/<slug>/`).
- It pauses the way a person does before reaching for the control.
- It finds the controls by their accessible role and name: role `link`, name `Message`, exact. The role and name are module constants, pinned to literals. Only links count: a `button` named **Message** is never a candidate and never clicked, and its presence doesn't refuse.
- It refuses unless every link named **Message** on the page, hidden or visible, names this contact:
  - its `href` path is `/messaging/compose/`, relative or on `www.linkedin.com`;
  - each of its query parameters appears exactly once;
  - its `profileUrn`, URL-decoded, is the contact's `urn:li:fsd_profile:<id>`, and its `recipient` is the same bare `<id>`.

  Zero links, or any link that names another profile, repeats a parameter, or holds no such `href`, means no click. The capture rendered three identical links, so a rule of "exactly one control" would refuse every profile. This rule accepts any number of links, but only when they all lead to the same compose for the same person.
- It clicks exactly one of them: the first visible one in document order, through a locator that matches the role, the name, **and** the contact's `href`, never a bare position such as `nth(i)` on the role locator. If none is visible, it refuses.
- It clicks once, at the control's own box, with a person's press length. Playwright scrolls the control into view first if it needs to; that's part of the click, not a separate input. A click that fails isn't tried again.

This binds the click to the contact by URN, not by where the control sits or how many there are. The draft's earlier rules (look only inside the top card; refuse more than one control) are replaced: the capture showed no landmark for the top card, and three controls on every profile.

The click opens a bubble on the profile page, and the tab doesn't navigate. Any change of the tab's URL after the click stops the run.

### After the click: no reattach, no navigation

Once `click_message` returns, the run never calls `ensure_page`, `goto`, `new_page`, `scroll`, or `observe`, and never reattaches. The compose option's answer comes from the observation opened before the click. A lost tab or browser ends the run: `not_typed` before the first key is attempted, `unknown` after. A reopened tab would be a new page with no verified composer, and a reattach could land keys in a tab nobody checked.

### Typing into one verified composer

`BrowserRun.type_into_composer` types the rendered body into the composer the click opened. Before the first key, it verifies all of these, and types nothing if any fails. Every count includes hidden elements (`include_hidden=True`), so a minimized bubble counts.

- **Exactly one composer is on the page.** The composer is found by role and name: role `textbox`, name `Write a message…` (with U+2026, the ellipsis character), exact, never by a CSS class or the `msg-form-…` id. It must sit in the bubble the recipient checks verified (see the next subsection). Any other composer on the page, such as a minimized bubble left from an earlier prefill or opened by the person, means more than one, and the prefill refuses with a reason that asks the person to close the other message bubbles. netkeeper never closes a bubble: closing one deletes its draft, and it's an input this ADR doesn't authorize.
- **The composer is empty.** It reads as empty under [the text rule](#reading-the-composers-text). The capture shows that a minimized bubble keeps its draft across pages, so the Message click can restore a bubble that already holds a draft for this contact. The prefill refuses, and the person clears the draft.
- **The composer's recipient is this contact.** See the next subsection.
- **The verified composer holds focus.** Focus is read through Playwright's own selector engine (a `:focus` match on the composer's locator), never through `evaluate` or other script of netkeeper's in the page. If the composer doesn't hold focus, the prefill types nothing (but see [decision 5](#focusing-the-composer-decision-5)).
- **No chunk holds a control character.** The method refuses, before the first key, a plan in which any chunk contains `\n`, `\r`, or any other C0 or C1 control character. Playwright's `keyboard.type` maps `\n` and `\r` to the Enter key, so a newline is only ever a `newline=True` step of the plan, pressed as Shift+Enter. `TypeStep`'s constructor already refuses such a chunk; this check doesn't rely on it.

#### The recipient

The composer's `aria-label` doesn't name the recipient, so the recipient comes from the Message links, from what the page loaded, and from the bubble. All three must pass. A contact without a public id (slug) can't be prefilled at all: the run navigates to `/in/<slug>/` and the checks read it. A stale slug fails safe: the profile redirects or doesn't load, the tab isn't on the contact's path, and nothing is clicked.

1. **The Message links.** Every link named **Message** named the contact's profile URN and bare id (checked in `click_message`).
2. **What the page loaded.** From the observation opened before the click, the run reads the compose option, `GET voyagerMessagingDashComposeOptions/<fsd_composeOption urn>`:
   - the first part of the `fsd_composeOption` URN in the request's path is the contact's bare profile id;
   - the answer's `data.composeNavigationContext.recipientUrns` is exactly one URN, the contact's `urn:li:fsd_profile:<id>`;
   - `composeOptionType` is `REPLY` with an `existingConversationUrn`, or `CONNECTION_MESSAGE` without one. Any other type, a `REPLY` without a conversation, or a `CONNECTION_MESSAGE` with one is refused.

   No compose option seen, or more than one, means `not_typed`. The type decides which bubble layout the next check expects.
3. **The bubble.** At most one `role="dialog"` named `Messaging` may be on the page in either layout. Then:
   - **`REPLY`, an existing conversation.** Exactly one `role="dialog"` named `Messaging` holds the composer. Its header `h2` holds exactly one link, and that link is `/in/<profile id>/`, where the id is the contact's bare profile id (the same id as in the URN, not the vanity slug). **The other layout** is any `New message` heading anywhere on the page; one refuses.
   - **`CONNECTION_MESSAGE`, never messaged.** This check doesn't rely on `role="dialog"`, which the capture didn't confirm. The scope is the innermost element that contains both the `New message` heading and the verified composer (for example, `locator("*").filter(has=heading).filter(has=composer).last`). Exactly one `New message` heading is on the page. Inside the scope there's exactly one chip (a button whose name starts `Remove `), exactly one recipient field named `Enter message recipients`, and exactly one `/in/` link, the card's, whose slug is the contact's public id. Zero chips, two chips, two cards, or a card for anyone else is refused. Scoping matters: the profile page around the bubble links its own slug too, so a page-wide search would find the right slug whatever the bubble said. When the profile page had exactly one `h1`, the chip's name must also be `Remove ` followed by that `h1`'s text, LinkedIn's own name for the profile, never netkeeper's stored name; a mismatch refuses. **The other layout** is a `role="dialog"` named `Messaging` that doesn't contain the `New message` heading; one refuses. A dialog that does contain it is this bubble's own root, so it never makes this layout refuse itself.

The bubble's DOM alone never authorizes typing, and neither does the compose answer alone. The draft's earlier rule ("no observed conversation means `not_typed`") is replaced: a contact you've never messaged has no conversation, and the compose option names the recipient either way.

For an existing conversation, the outcome's `conversation_urn` comes from `existingConversationUrn`. Its thread id is the same one the page's following `messengerMessages` request names, so P4-03 reports it in the form the inbox poll (P4-01) matches.

#### Reading the composer's text

The composer is a `contenteditable` element, and its text is read with Playwright locator reads, never with script:

- Each paragraph (`p`) inside the composer is read with `inner_text`. A paragraph boundary is one newline, and a `<br>` inside a paragraph is one newline.
- A paragraph that holds only a `<br>` is an empty line, and a trailing `<br>` at the end of a paragraph adds nothing. So the empty composer the capture shows, `<p><br></p>`, reads as the empty string.
- A no-break space (U+00A0) reads as a space. A browser may write one for a typed space.
- Anything in the composer the rule can't read this way, such as an element other than `p` and `br`, makes the text unreadable. Unreadable text fails the check.

The expected text is the typed prefix with each newline step as `\n`. P4-03's smoke replica uses a real `contenteditable`, so the rule is tested against a browser's own editing, not a fake.

#### The replay

Typing replays the plan: lognormal delays, median 140 ms a character, no typos, a 300-second ceiling. Each step runs in this order: **the delay, then the checks, then the key**, with nothing awaited between the checks and the key.

- A space is sent with `keyboard.insert_text`, never `keyboard.type`. Playwright's `keyboard.type` maps a space to the Space key, which activates a focused button. So the only key that could activate a focused button is Shift+Enter.
- Any other single printable ASCII character is typed with `keyboard.type`.
- Any other chunk (`TypeStep.needs_insert_text`: an accented letter, an emoji sequence) is sent with `keyboard.insert_text`.
- A `newline=True` step is pressed as Shift+Enter.

The run checks for cancel at each step's checks, and stops when the tab closes or its URL changes.

**The point of no return** is the first key call attempted: the first `type`, `insert_text`, or `press`, whether or not it returned. From then on, any stop, including an exception from a check's read, ends the run as `partially_typed` (a check failed cleanly) or `unknown` (the run can't tell what the composer holds), never `not_typed`. Before that point, every refusal and every exception is `not_typed`.

### Checked before every key

The checks above don't run only once. Playwright sends keys to whatever has focus and doesn't check where that is. If focus moved mid-type, to the Send button for example, a Shift+Enter could send the message, and the rest of the body could land in the wrong place. In the never-messaged layout, focus could also move to the recipient field, and the rest of the body would become a people search.

- Right before every chunk and every Shift+Enter, after the step's delay, `type_into_composer` checks again that exactly one composer is on the page, that the bubble's recipient is unchanged (the dialog's header link, or the one chip and its card's link in the scope), that the verified composer holds focus, that the tab's URL is unchanged, and that the composer's text equals the prefix typed so far.
- Any failed check stops typing at once, with the outcome `partially_typed`.
- Before the hand-over, the composer's text must equal the body, or the outcome is `partially_typed`.
- The prefill UI tells the person not to type or click in Chrome while the prefill types.

### Focusing the composer (decision 5)

Where focus lands after the Message click is unknown (#429, item 5). The maintainer chooses one option at acceptance; either is a one-line change to this subsection, and the rest of this ADR holds under both.

- **Option A: no focus input.** netkeeper never focuses the composer. If focus isn't in it after the recipient checks, the prefill ends `not_typed`. If #429 shows focus doesn't land there, every prefill refuses until an amendment adds option B.
- **Option B (the safety review's recommendation): one `Locator.focus()`.** `type_into_composer` may call `focus()` on the verified composer's locator, never a click. It's called at most once per run, only after every recipient, emptiness, and single-composer check has passed, and only when the composer doesn't already hold focus. Focus is then checked again, and if the composer still doesn't hold it, the prefill ends `not_typed`. The call is pinned in `ALLOWED_INPUTS` like the others.

**Chosen: to be decided at acceptance.**

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
| Off | Command+Enter | sent |

Shift+Enter never sends, whatever the setting. So:

- The prefill types each newline as Shift+Enter. That's the only key it ever `press`es.
- It never presses Enter, NumpadEnter, Ctrl+Enter, or Command+Enter (Meta+Enter), and never holds a modifier down. Enter sends with the setting on; Command+Enter sends with it off.
- It never clicks Send, and never locates the form's `button[type="submit"]`.

**Multi-line templates.** P4-11 (#377) lints a LinkedIn body's newlines (CR, LF, or CRLF) as an error while `pacing.SHIFT_ENTER_NEWLINES_ALLOWED` is false, and `typing_plan` refuses them under the same flag. That flag is the single source of truth. P4-03 sets it to true in the same pull request that adds the Shift+Enter press and its pins, and multi-line LinkedIn templates then lint clean. That pull request also updates the tests that pin the flag false (`tests/test_pacing_typing.py`, about line 159, and `tests/test_template_render.py`, about line 948). Every other line break (VT, FF, NEL, U+2028, U+2029, and the rest of `LINE_BREAK_CHARS`) stays refused, whatever the flag says. (#382 calls the flag `LINKEDIN_ALLOW_NEWLINES`; the name P4-11 shipped is `SHIFT_ENTER_NEWLINES_ALLOWED`.)

The Send click isn't authorized here. Auto-send (P4-04, #384) needs its own amendment, gated on `[campaigns] linkedin_auto_send` and the step's `auto_send` mode, and is built only after CP8 signs off.

### Handing the tab over

At the end of a prefill run (spec 11.6), `BrowserRun.hand_over()` drops the run's reference to its tab and detaches without closing it, so the provider's later `close()` closes nothing. It changes no focus (see [Bringing the tab to the front](#bringing-the-tab-to-the-front)). From then on the tab isn't netkeeper's: no later run reuses it, navigates it, or closes it. `hand_over` is a new ending, distinct from `close()`, and is reached only from `netkeeper/linkedin/page_messaging.py`. The run releases the browser lock after typing; it doesn't wait for the send.

Every run that got as far as the Message click hands its tab over, whatever the outcome:

- **After typing,** the bubble holds the body or part of it. The capture shows it keeps its draft if the person minimizes it or moves to another page.
- **After a `not_typed` refusal that followed the click,** the bubble is open and empty. Closing it would be safe, since it holds no draft, but it's an input this ADR doesn't authorize, and closing the tab may not clear it, because a bubble persists across pages. So the tab is handed over like any other, and the UI shows the refusal's reason and asks the person to close the empty bubble.

Because the next prefill refuses a page with more than one composer, the person closes that bubble (after sending, or to discard the draft) before the next prefill. P4-09 allows one open prefill at a time.

**Confirming the send.** The next inbox poll (P4-02, #381), or the "I sent it, check now" action, confirms the send by matching the first outbound message in the conversation dated after `prefilled_at`. So `prefilled_at` must be a time before the first key: the moment typing starts, taken before the first key call (#416's review, #382's comments). P4-09's `record_prefill_outcome` gets an explicit `prefilled_at=` argument, which P4-03 adds and passes, instead of reusing the `now` it records the outcome at. P4-03 also corrects `CONFIRM_SKEW`'s docstring in `netkeeper/services/campaign_replies.py` (about line 186), which says `prefilled_at` is taken when the outcome is recorded. If `prefilled_at` were later than the person's click on Send, or the clocks disagreed by a few seconds, the send would never be confirmed.

After a failure mid-type, the tab still holds what was typed:

- The UI flags `partially_typed` and `unknown` with "Part of a message is in the composer and may be kept as a draft. Clear it, or close the message bubble, which deletes the draft."
- netkeeper never clears it. Clearing is an input this ADR doesn't authorize.
- If the run crashes, the message stays claimed, and P4-09 lists it as interrupted under "waiting for you", holding the one open slot until the person discards it. It's never claimed or typed again.

### The pins

P4-03 (#382) changes `tests/test_browser_safety.py` in the same pull request as the code these pins guard, not in this one.

Static pins:

- `ALLOWED_INPUTS` entries for exactly two methods: `click` in `BrowserRun.click_message`; and `keyboard` (read once, into a local), `type`, `insert_text`, and `press` in `BrowserRun.type_into_composer`. Under option B of decision 5, `focus` in `type_into_composer` too, at one call site.
- A literal check that the only `press` argument anywhere is `"Shift+Enter"`.
- No `keyboard.down` and no `keyboard.up` anywhere. No `focus()` anywhere, except option B's one call.
- `keyboard.type` is never called with a space, or with a chunk that isn't one printable ASCII character other than a space.
- The Message link's role and name (`"link"`, `"Message"`), the composer's role and name (`"textbox"`, `"Write a message…"`), the dialog's name (`"Messaging"`), the `"New message"` heading, the `"Enter message recipients"` field, and the `"Remove "` chip prefix are module constants pinned to literals.
- No string argument to a locator-building call (`get_by_role`, `get_by_text`, `get_by_label`, `get_by_title`, `locator`, `filter`, or a `has_text` or `name` keyword) under `netkeeper/linkedin/` matches "send" or "submit", ignoring case.
- The `click` in `click_message` is on a locator built with the contact's `href`, never on a bare `nth` of the role locator.
- No code under `netkeeper/` passes `allow_newlines=` to `typing_plan`.
- `pacing.SHIFT_ENTER_NEWLINES_ALLOWED` is true only together with the Shift+Enter press pin.
- `bring_to_front` is called at one site, inside the prefill's start, before `click_message`; a runtime pin checks that it's called once per run.
- `netkeeper/linkedin/page_messaging.py` in `BROWSER_MODULES` and `BROWSER_CALLERS`.
- `hand_over` reachable only from `page_messaging.py`.

Runtime pins, against a fake page:

- A plan with a `"\n"` chunk gives `not_typed` with zero keys.
- A `typing_plan` failure, including an unexpected exception, gives `too_long` or `not_typed` with zero keys, no navigation, and no budget spent.
- A busy browser lock gives `not_typed` with zero navigation and zero budget spent.
- A claim older than N seconds (by `Message.scheduled_at`) gives `not_typed` with zero navigation and zero budget spent.
- Three Message links with the contact's `href` give one click, on the first visible one, through a locator that matches the `href`.
- A link named Message that names another profile, mismatched `profileUrn` and `recipient`, a repeated query parameter, no link, or no visible link gives `not_typed` with no click.
- A `button` named Message beside the links is ignored: the prefill proceeds and never clicks it.
- The compose-option observation is opened before the click; after the click, no `observe`, `scroll`, `ensure_page`, `new_page`, or `goto` is called.
- A compose option whose path id or `recipientUrns` isn't the contact's, with more than one recipient, with an unknown `composeOptionType`, or missing, gives `not_typed`.
- A `REPLY` bubble whose header `h2` links to another profile id, or holds two links, gives `not_typed`. A `REPLY` page with any `New message` heading gives `not_typed`.
- A `CONNECTION_MESSAGE` page with a `Messaging` dialog that doesn't contain the `New message` heading gives `not_typed`. One whose bubble root is a `Messaging` dialog containing the heading proceeds.
- Two `Messaging` dialogs, including a hidden or minimized one, give `not_typed` in either layout.
- A never-messaged bubble with zero chips, two chips, two `New message` headings, two `/in/` links in the scope, or a card for another slug gives `not_typed`, with or without a `role="dialog"` root. A page whose profile links the contact's slug while the bubble's card links another slug gives `not_typed`.
- A chip whose name doesn't match the profile's `h1` gives `not_typed`.
- A second composer on the page before the first key, including a hidden one, gives `not_typed`; one appearing mid-type stops typing.
- A non-empty composer gives `not_typed`; `<p><br></p>` reads as empty; a no-break space reads as a space.
- Focus not in the composer before the first key gives `not_typed`; focus moving away after chunk k gives zero further keys. Under option B, `focus()` is called at most once, only after the checks, and focus is checked again after it.
- A space is sent with `insert_text`, never with `keyboard.type`.
- No delay is awaited between a step's checks and its key.
- A URL change before the first key gives `not_typed`; a URL change mid-type gives `partially_typed` with zero further keys.
- A cancel mid-type gives `partially_typed` with zero further keys.
- An exception from a check's read after the first key call was attempted gives `partially_typed` or `unknown`, never `not_typed`.
- Composer text that diverges from the typed prefix stops typing.
- A recipient (header link, or chip and card) that changes mid-type stops typing.
- A final composer text that differs from the body gives `partially_typed`, never `prefilled`.
- A `prefilled` outcome records a `prefilled_at` no later than the first key call.
- A `not_typed` refusal after the click hands the tab over and leaves the bubble open.
- After `hand_over()`, the provider's exit leaves the page open.

P4-03's loopback replica (`tests/smoke/test_prefill_smoke.py`) copies the capture's layout:

- three Message links with the same compose `href`, one visible;
- decoys: a link named Message that names someone else (the prefill refuses) and a button named Message (the prefill proceeds and never clicks it);
- a compose option answer;
- both bubble layouts, the never-messaged one without a `role="dialog"` root, inside a profile page that links the contact's own slug;
- a real `contenteditable` composer, a submit button, and an Enter-to-send toggle that records a "sent" event on a bare Enter when it's on and on Command+Enter when it's off.

## Consequences

- netkeeper can type a message into LinkedIn, and no key or click of netkeeper's is meant to send one. What the user sees in the handed-over tab is the rendered body, in the right conversation, waiting for their click.
- Multi-line LinkedIn templates become possible once P4-03 lands, typed with Shift+Enter, which the capture shows never sends.
- The recipient probably sees "typing…" while the prefill types, and for up to 300 seconds even if the person never sends. The maintainer accepted this for the initial release (#430 tracks a possible toggle).
- Every refusal before the first key call leaves nothing typed (`not_typed`), so a refused prefill costs at most a budget unit, not a wrong message. A prefill interrupted after it (`partially_typed`, `unknown`) is never retried: retyping into a composer that may hold half the message is worse than a person finishing it. The person clears what's there.
- Each prefill spends one `li_prefills` unit and one `profile_visits` unit before it navigates (P4-03, P4-09).
- A prefill that can't get the browser lock at once doesn't start, and a claim that waits too long lapses. A person may have to click **Prefill** again.
- Any other message bubble on the page stops the prefill before the first key. The person closes earlier bubbles, including the last prefill's and any empty one a refusal left, before the next one.
- A contact without a public id can't be prefilled.
- **The remaining risk is a send.** Focus is checked right before each key, not atomically with it. If focus moves to the Send button in the moment between the check and a Shift+Enter, the Shift+Enter could activate it and send what's typed so far. A space can't, because it's inserted as text, and other characters don't activate a button. A character landing elsewhere is stopped by the next step's checks. This window is the reason the person watches and leaves Chrome alone.
- Under option A of decision 5, if focus doesn't land in the composer after the Message click, every prefill ends `not_typed` until an amendment adds option B.
- Handed-over tabs accumulate in the user's Chrome until the user closes them. That's deliberate: netkeeper closing a tab is how a draft would be lost.
- The person must leave Chrome alone while the prefill types. Touching it stops the prefill as `partially_typed`, which is the safe direction.
- If LinkedIn changes the Message control, the compose option, the composer, or how the bubble shows its recipient, the prefill refuses and types nothing. That's the intended failure.
- If LinkedIn changes what Shift+Enter does, nothing in this ADR detects it at run time. The capture is the evidence, and a later capture that contradicts it means newlines are refused (the flag goes back to false) until a new amendment.
- A future contributor keeps these true: the only clicks are Contact info and Message, each once, each bound to the profile it's on; the only typing is aimed at one verified composer for the contact, with focus and the composer checked again right before every key; the only key pressed is Shift+Enter; no code path presses Enter or clicks Send; and a handed-over tab is never touched again.

## Decisions the maintainer makes at acceptance

1. **Where the tab comes to the front.** This ADR brings it to the front once, at the run's start, and `hand_over()` changes no focus. That adjusts the "brought to the front" wording of 2026-10-03.
2. **N, the claim lapse.** How many seconds after `Message.scheduled_at` a claim may wait to start before it lapses back to the queue (suggested: 60).
3. **Other open bubbles.** This ADR refuses a page that shows any composer besides the one the click opened, so the person closes earlier bubbles, including the last prefill's, before the next prefill. The alternative, allowing minimized bubbles for other people and relying on the focus and recipient checks, is looser.
4. **The Message click rule.** The profile renders three identical Message links. This ADR requires every link named Message to name the contact's URN and bare id, then clicks the first visible one, once. It replaces the draft's "exactly one, in the top card" rule.
5. **Focusing the composer.** Option A (no focus input) or option B (one `Locator.focus()` after every check), as [described above](#focusing-the-composer-decision-5).

The typing indicator, a decision in the draft, was settled on 2026-10-05: accepted, with a possible toggle later (#430).
