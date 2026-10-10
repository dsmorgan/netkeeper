# 0007. The prefill's inputs: one Message click, typing into one verified composer, never Enter

Date: 2026-10-03, updated 2026-10-05 with the P4-06 messaging capture (#374), accepted 2026-10-06, amended 2026-10-06 for #444 (which Message control is clicked), amended 2026-10-07 for #470 (scrolling back up to a covered control), amended 2026-10-08 for #473 (the Message check, a dry run up to the click)

## Status

Accepted (2026-10-06)

The maintainer accepted this ADR in review on 2026-10-06, with the [decisions recorded at acceptance](#decisions-recorded-at-acceptance). The facts below come from that analysis (PR #432), which corrects the first summary on #374 in several places: the profile renders three Message controls, the existing conversation's header links by profile id rather than by slug, and the compose requests name the recipient.

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

- **Whether the composer holds keyboard focus right after the Message click** (#429, item 5). The shape doc expects P4-03 to focus the composer itself. netkeeper may, once, under [Focusing the composer](#focusing-the-composer). A composer without focus after that gets no key, and the prefill ends `not_typed`.
- **Which Message control is visible** (#429, item 6). The click rule below doesn't depend on it.
- **Whether a half-typed desktop draft appears in the phone app** (#429). No draft-save request appeared while typing, so the capture gives no sign that a draft leaves the browser; no check here depends on it.
- **Whether the never-messaged bubble's root is `role="dialog"`.** The checks for that layout don't rely on it.

## Decision

netkeeper may give a LinkedIn page exactly three new inputs, all in `BrowserRun` (`netkeeper/linkedin/browser.py`): one click on Message, typing into one verified composer, and at most one `Locator.focus()` on that composer. It also gets one new way to end a run. Everything else in ADR 0006 stays: no script in the page, no request of netkeeper's own, and no interception. The run also brings its tab to the front once, at the start (see below).

### Who triggers it

A prefill runs only when a person asks for it, from the "ready to prefill" queue, while watching Chrome. P4-09's (#379) `claim_prefill` writes the message `scheduled`, stamps its `Message.scheduled_at` with the claim's time, records a manual `message_send` run, and submits it; `create_run` refuses a scheduled `message_send` run.

A prefill run is never retried and never resumed. Before it spends any budget or navigates, its runner settles two things, and each ends the run as `not_typed`, so P4-09 gives the claim back and the message doesn't stay interrupted, holding the one open slot:

- **The browser lock.** The run asks for it with `wait=False`. If another run holds it, the run records `not_typed` ("the browser was busy").
- **The claim lapse.** If more than 60 seconds have passed since `Message.scheduled_at`, the run records `not_typed` ("the claim lapsed"). So a prefill never starts long after the person who asked has stopped watching. The 60 seconds is a module constant pinned to its literal. P4-09 has no lapse of its own.

Both park the enrollment like any other `not_typed`. Since #445 nothing claims a `not_typed` step again on its own: a person clicks **Try again**, and after a run that clicked Message (or one that can't say it didn't), confirms that no bubble for that contact is open. That retry is a new prefill run, under every rule here; it never resumes the one that refused. See [architecture section 11.6's #445 note](../architecture.md).

### The typing plan comes first

The runner builds the whole plan with `pacing.typing_plan` (P4-10, #376) before it spends any budget or navigates, as `typing_plan`'s docstring requires. A plan the module refuses costs nothing and types nothing:

- `TypingTooLong` is `too_long`.
- Every other `TypingPlanError` (`MultilineRefused`, `UnsupportedCharacter`, `TypingPlanMismatch`, `InvalidTypingProfile`) is `not_typed`. Lint and the fire path's rendered-message check (#382's comments) should stop these bodies first; this is the backstop.
- Any other exception from `typing_plan` is wrapped in a distinct `TypingPlanError` subclass (for example, `PlanInvariantBroken`). The wrapper catches `Exception` only, raises with a fixed message that never quotes the text, and chains the cause with `from`. The prefill handles it as `not_typed`, never retries, and logs only the exception's class. No lint rule matches it, so the lint and plan agreement fuzz can assert that it never fires (#426's review).

The runner calls `typing_plan` without `allow_newlines=`, so the flag's default, `pacing.SHIFT_ENTER_NEWLINES_ALLOWED`, decides. That default is bound when the function is defined, so the module constant in source is the only switch: no code under `netkeeper/` passes `allow_newlines=` (a pin below), and a test that patches the constant at run time doesn't change what `typing_plan` does.

### Bringing the tab to the front

The run brings its tab to the front once, at the start, before `click_message`, while the person is watching. That's the only `bring_to_front` call in the package. Nothing later in the run changes which tab or window is in front, and `hand_over()` doesn't either: a change while the person is reading or typing elsewhere could send their keys somewhere they didn't intend.

This adjusts the maintainer's "brought to the front" wording of 2026-10-03, which placed it at the hand-over. The maintainer accepted the change on 2026-10-06.

### One click on Message

Before the click, the run reads the profile page's `h1`, if there's exactly one, for the chip check below. It also opens the observations (`BrowserRun.observe`, ADR 0006) for the two requests the click causes: the compose option, and the page's own `messengerMessages` thread request. So neither answer can arrive before anyone listens.

`BrowserRun.click_message` clicks the **Message** control once per prefill, on the contact's profile, modeled on `BrowserRun.click_contact_info`:

- It refuses when the run's tab is gone, and when the tab isn't on the contact's profile path (`/in/<slug>/`).
- It pauses the way a person does before reaching for the control.
- It finds the controls by their accessible role and name: role `link`, name `Message`, exact. The role and name are module constants, pinned to literals. Only links count: a `button` named **Message** is never a candidate and never clicked, and its presence doesn't refuse.
- It refuses unless every link named **Message** on the page, hidden or visible, names this contact:
  - its `href` path is `/messaging/compose/`, relative or on `www.linkedin.com`;
  - each of its query parameters appears exactly once;
  - its `profileUrn`, URL-decoded, is the contact's `urn:li:fsd_profile:<id>`, and its `recipient` is the same bare `<id>`.

  Zero links, or any link that names another profile, repeats a parameter, or holds no such `href`, means no click. The capture rendered three identical links, so a rule of "exactly one control" would refuse every profile. This rule accepts any number of links, but only when they all lead to the same compose for the same person.
- It clicks exactly one of them, through a locator that matches the role, the name, **and** the contact's `href`, never a bare position such as `nth(i)` on the role locator. Which one is set by [Choosing the control](#choosing-the-control) (#444). If none is visible, it refuses.
- It clicks once, at the control's own box, with a person's press length. Playwright scrolls the control into view first if it needs to; that's part of the click, not a separate input. A click that fails isn't tried again. Its log line and the run's counts record a fixed category for the failure (`intercepted`, `outside_viewport`, `not_visible`, `not_stable`, `detached`, `timeout`, or `other`), read from Playwright's actionability log, never the exception's text.

This binds the click to the contact by URN, not by where the control sits or how many there are. The draft's earlier rules (look only inside the top card; refuse more than one control) are replaced: the capture showed no landmark for the top card, and three controls on every profile.

#### Choosing the control

*Amended 2026-10-06 for #444.* CP8's first prefill worked, and the click raised on every one after it. On an isolated Chrome, Playwright's click raises the same way, after its 10-second wait, when the control it's given is one Playwright calls visible but can't click:

- a fixed copy, such as a sticky header that slides in on scroll, that sits outside the viewport (`element is outside of the viewport`);
- a control with something over its click point, such as a message bubble or the Messaging bar (`intercepts pointer events`);
- a control that keeps moving (`element is not stable`).

"The first visible one in document order" gives Playwright such a control whenever one comes first. So the click chooses among the visible links that passed the rule above, in this order:

1. **The top card's control**, when it's wholly inside the viewport and nothing is over its center. The top card's control is the first Message link after the profile's one `h1`, in document order (`xpath=following::a` from that `h1`, narrowed to the href-bound locator). With zero `h1` elements, or two or more, there is no top card.
2. **Any other control** that is wholly inside the viewport with nothing over its center, in document order.
3. **The top card's control, when it isn't wholly inside the viewport**: Playwright scrolls it into view as part of the click. A top card that is on screen but covered is never chosen this way.
4. Otherwise the click refuses, with no click: "no Message control is on screen with nothing over it". The run ends `not_typed` before any input, and no bubble opens.

"Nothing over its center" is a hit test at the point Playwright's click would press: the middle of the control's first content quad, clipped to the viewport, with an area over 0.99 square pixels (Playwright's own rule; a link that holds a tall icon has more than one quad, and its box's center can lie in none of them). The element the page would hit there must be the control's link or an element inside it, matched by DOM node, not by size, because an icon may overflow its link's box. Another copy of the link over this one counts as covering it, as Playwright's check would. The hit test and the viewport come from the page's own geometry, read over a DevTools session on the run's tab, with no script in the page:

- `Page.getLayoutMetrics`, for the layout viewport's size, which Playwright doesn't know for a tab it attached to, and how far the page is scrolled;
- `DOM.getDocument` (depth 0), `DOM.querySelectorAll` for the links with each verified compose `href`, and `DOM.describeNode` for each one's subtree: which nodes belong to which link;
- `DOM.getBoxModel` and `DOM.getContentQuads` for each link: its box, matched to Playwright's box for the control, and its click point;
- `DOM.getNodeForLocation` at that point, with `ignorePointerEventsNone`, which answers as `document.elementFromPoint` does. The quads are in viewport coordinates and this method takes document ones, so the point is sent with the page's scroll offset added (#470).

The session is opened in `BrowserRun._read_click_geometry`, sends only those seven read-only methods (and, since #490, `DOMSnapshot.captureSnapshot`; see [A pseudo-element at the click point](#a-pseudo-element-at-the-click-point)), describes at most 32 links per `href` (`MESSAGE_MAX_LINKS`; hidden copies count, so the cap sits far above the three the capture rendered), gives up after 3 seconds (`GEOMETRY_TIMEOUT_S`, so a hung renderer is a failed read), and detaches before the click. They change nothing the page can see, run no script, and dispatch no event. A session that can't open, or a read that fails, chooses without geometry: the top card's control, else the first visible one, as before #444. Playwright's own actionability checks still run at the click and refuse a covered or off-screen control, so the hit test only chooses; it never makes a click land where Playwright's checks wouldn't.

`click(trial=True)` was rejected for the hit test: it moves the mouse over the control (a hover the page sees) and scrolls, which are inputs this ADR doesn't authorize.

#### A pseudo-element at the click point

*Amended 2026-10-10 for #490.* On a real Chrome, `DOM.getNodeForLocation` over a pseudo-element answers with the pseudo-element's own `backendNodeId`, not its host's. Since #475, the link's own `::before`, `::after`, and nested ones such as `::before::marker`, and those of every element inside the link, count as the link's: `DOM.describeNode` with `depth: -1` lists them under `pseudoElements`. A `::first-letter` is different. On Chrome 154, the hit over a link's first letter is its `::first-letter`, with no `nodeId`. `DOM.describeNode` doesn't list it under its host's `pseudoElements`, and describing it by its id gives no parent. So the hit test read the link as covered and refused. That was a false refusal, never a false click.

When a hit isn't the link's by `describeNode` and Chrome gave it no `nodeId` (0, as every `::first-letter` hit had on Chrome 154; an ordinary cover such as the sticky header has one), the geometry read now sends one more read-only method, once per read: `DOMSnapshot.captureSnapshot` with `computedStyles: []` and no other parameter (`BrowserRun._pseudo_hosts`). Its answer lists every pseudo-element Chrome lays out, `::first-letter` included, with its parent. A hit on a pseudo-element counts as the link's when its nearest ancestor that isn't a pseudo-element is the link or inside it. Anything else at the point still covers the link, another element's `::first-letter` included. The snapshot runs after the hit tests, with its own limit of 2 seconds (`PSEUDO_HOSTS_TIMEOUT_S`). If it fails or is slow, the hit still counts as covering; it never makes the click go unchecked. `DOM.describeNode` by `backendNodeId` was rejected: it's a new parameter, and it still gives no host. The snapshot's answer holds the page's text and attribute values, input values included, and layout bounds. netkeeper keeps only the node ids and parent links from it, and logs none of it. It runs no script, dispatches no event, and changes nothing the page can see.

A `::first-letter` on a block around the link, one whose first letter is the link's, still reads as covering: its host is outside the link. That is a refusal, never a false click.

#### Scrolling back up to a covered control

*Amended 2026-10-07 for #470.* The prefill scrolls briefly before the click (one or two wheel steps of 120 to 360 pixels). On LinkedIn, a sticky header with its own **Message** control slides in at the top once a profile scrolls a little. A brief scroll can leave the top card's control inside the viewport but under that header: covered, so step 1 passes it over, and on screen, so step 3 doesn't apply. The header's own control isn't a clear candidate (its shape was never captured, #429), so the click refuses. CP8 saw that refusal three times in a row on one contact.

A second cause showed up while reproducing this on an isolated Chrome. `DOM.getContentQuads` answers in viewport coordinates, but `DOM.getNodeForLocation` takes document coordinates: Chrome subtracts the scroll offset from the point it's given. Before #470, the hit test sent the viewport point unchanged. So on any scrolled page it tested a point above the control, by the scroll's depth. That point either held some other element (the control read as covered, and the click could refuse) or lay above the viewport (the read failed, and the click went unchecked). Every page in #444's tests had a scroll offset of 0, so none of them showed it. The hit test now adds `cssLayoutViewport`'s `pageX` and `pageY` to the point. The method and its parameters are unchanged.

So, after the brief scroll and before the click, `PagePrefill.prefill` asks `BrowserRun.message_cover`, a read with no input, whether the click would refuse because the top card's control is on screen but covered and no other control is clear. That read makes the same reads the click makes, in the same order (decision 3's bubble check, decision 4's href check, the visible controls, and the geometry read above, in its own session, with the same seven methods), and answers with a fixed category. When it says covered, the prefill scrolls back up once, the way a person scrolls back to a button they can't reach: `scroll_back_to_top`'s upward wheel steps, the size of the brief scroll's (120 to 360 pixels), until they cover the brief scroll's depth and one step more, then a short look. The click then reads the page again and chooses as above; when nothing is clear it still refuses, with no click.

- The scroll back comes before the click, so the rule that nothing scrolls after the click holds (#456).
- The click stays bound to the contact's verified `href`: the top card's control or another verified, clear candidate. netkeeper doesn't click the sticky header's own control unless its shape is verified by a capture.
- An off-screen top card isn't covered: step 3 still lets Playwright scroll it into view as part of the click.
- The brief scroll's size is unchanged. Bounding it so it can't park the top card under the header would need the header's height and the top card's position on the real page, which no capture shows.

A refusal logs why, as a fixed category, never a name, a slug, a URL, or the page's text: `no_candidates`, `no_top_card`, `top_card_off_screen`, `covered_by_sticky_header`, `covered_by_bubble`, or `covered_by_other`. The hit test learns only that the element at the click point isn't the control's own, not what it is, so the cover is placed by where that point sits: within 160 pixels of the viewport's top is the sticky header (or the global navigation above it), within 64 pixels of its bottom is the Messaging bar or a bubble, and anywhere else is something else.

The run's counts record which control was chosen (`message_click_target`: `top_card`, `on_screen`, `top_card_off_screen`, or `unchecked`) and, for a click that raised, `message_click_failure`.

The click opens a bubble on the profile page, and the tab doesn't navigate. Any change of the tab's URL after the click stops the run.

#### Checking the Message click without clicking

*Amended 2026-10-08 for #473.* After #470, a live prefill still refused with `MESSAGE_NOT_ON_SCREEN` on a profile with no bubble open and a normal **Message** button in its top card. Each try spent a prefill and said one category word. `netkeeper linkedin message-check <contact id>` runs the prefill's steps up to the click and stops, so the next failure can be diagnosed in one run.

- It takes the prefill's gates: active hours, the session flag and heat before the run is recorded, and again under the browser lock, which it never waits for. Its run is a `message_send` run started by hand through its own gate token (`runs.MESSAGE_CHECK_GATE`), so the scheduler's gap after a prefill and auto-send's spacing count it, and no prefill starts while it runs. It claims no message, so no message or step changes. It spends one `profile_visits` unit before the navigation, as a prefill does, and no `li_prefills` or `li_messages_auto` unit. A wall at the profile sets the session flag or raises heat, as a prefill's does. The run ends `completed` with stop reason `message_check`.
- `PageMessageCheck.run` calls the same `BrowserRun` methods as `PagePrefill.prefill`, in the same order, up to the prefill's heading read: `bring_tab_forward`, `goto`, the brief `scroll`, `message_cover`, and the scroll back when the cover is covered. Then it waits the click's pause. It never reaches `click_message`, `type_into_composer`, `observe`, any page input, or script in the page. The provider closes its tab at the end, as any run's.
- It reads the page three times, right after the load, after the brief scroll, and when the click would read it, through `BrowserRun.message_check_snapshot`. That method first makes the click's own reads, through the click's own code (decision 3's bubble check, `_find_message_controls`, `_read_click_geometry`, and `choose_message_target`), so the verdict it reports is the click's. Then it reads more: the `h1` count, every link and button named exactly "Message", hidden ones counted, with each one's tag, visibility, position after the `h1`, and the shape of its `href` in fixed words, and a count of names that only start with "Message".
- One more CDP session per read (`BrowserRun._read_check_detail`, with its hit test in `_check_hit`) sends four of the seven read-only methods above: `Page.getLayoutMetrics` (the viewport, the scroll, the zoom, and device pixels per CSS pixel), `DOM.getDocument` with `depth: -1` (the tree once, so a hit element's tag, role and ancestors are looked up locally; pseudo-elements, and any shadow roots and frame documents the answer holds, are walked with their host as parent, and a hit on an element the tree doesn't hold reads as inside a frame, when its `frameId` is a frame owner's or isn't the one on the document's `<html>`, or else inside a shadow root. The check never sends `pierce`, so Chrome lists a shadow root or a frame's document without expanding its children, and only those root nodes are indexed), `DOM.getNodeForLocation` at each candidate's click point and each on-screen control's center, and `DOM.getBoxModel` for ancestors of a hit whose tag is the control's. The allowlist of methods and parameters is unchanged. Since #490, a hit the tree doesn't hold, outside a frame, is looked up in the same `DOMSnapshot.captureSnapshot` read the click makes, at most once per read: a `::first-letter` reads as a pseudo-element of its host, inside the control or covering it, not as inside a shadow root. When the snapshot fails, the hit reads as before, and the failure is listed.
- The report goes to the terminal only. It holds numbers, fixed words, and tag and role names (a role from WAI-ARIA's list, else `other`; never an `aria-label`), with each read's time since the load: never a name, a slug, a URL, an `href` value, or the page's text. A read that fails is listed by its exception's type. Nothing it reads is stored; the run row gets fixed words, or an exception's type name when the check fails.

`tests/test_browser_safety.py` pins that the check, and every method and function it reaches, reaches no input, no script, and no observation; that its only `bring_to_front` is `bring_tab_forward`'s, as the prefill's before its click; that its calls are the prefill's up to the click; and that it sends only from the allowed CDP sites.

#### Checking a bubble's close control without clicking

*Amended 2026-10-10 for #495.* The first scheduled auto-send (#479, run 134) landed its Send, and then `close_sent_bubble` refused: the bubble "does not have one close control for this person" (ADR 0008, D1). `netkeeper linkedin message-check <contact id> --bubble` reads the shape of the close control in a bubble you opened by hand, so the next safety PR can fix the match from the live shape.

- If the tab auto-send left open is still open, run `--bubble` against it before you close anything: it shows the bubble's state right after a send. Otherwise open the contact's message bubble by hand in the netkeeper Chrome window first. An exact match on a bubble you opened by hand means the mismatch comes from the bubble's state right after a send, not from the match itself.
- The command takes the same gates, run row, browser lock, and port as `message-check`, so it needs active hours, and its run counts toward auto-send's spacing (and the gap after a prefill). It opens no tab and no profile, so it spends no `profile_visits` unit. Under the lock it checks the session flag, heat, and a cancel again, and a cancel once more after its read; a cancel ends the run `aborted`. Otherwise the run ends `completed` with stop reason `bubble_check`.
- It finds the tab that holds the bubble instead of opening the profile again: of the context's open tabs, it reads only those on LinkedIn's origin, and a tab is the contact's when one of its `Messaging` dialogs has exactly one header link, to `/in/<profile id>/`. With no such tab it reports the counts and stops; with more than one, it says how many and reads the first. It never navigates, scrolls, brings a tab forward, clicks, types, or focuses, and it opens no CDP session of its own.
- In that dialog it reports, in counts and fixed words: the buttons whose name starts with `Close your conversation with `, hidden ones counted, how many are visible, how many match without hidden text, and how many are on the whole page; the count by the exact name the close lookup used before #499; the close lookup's own rule count since #499 (1 or 0, and why it found none, `close lookup by the rule: 1`); buttons whose name starts with "close" in any case, and the never-messaged bubble's `Close your draft conversation` and `Minimize your conversation`; and the prefix counts of the options and minimize buttons. For each close button (the first four), it says how the name past the prefix relates to the header link's name (exact, equal after whitespace normalization, equal after NFC normalization, differing only in case, one starts with or contains the other, or unrelated), with the signed difference in characters, and, for a visible button, whether hidden text changes its name. For the header link, it says where its name came from (its `aria-label`, its text, or its text without its `aria-hidden` parts), how many elements and `aria-hidden` elements are under it, how much longer its text is than its name, and whether NFC normalization changes the name the lookup asks with.
- `close_sent_bubble`'s refusal carries the same shape in shorter words, in parentheses after its unchanged words, so a run's note says which way the lookup missed. The header link's details come first, then the counts, then the first two close buttons, and `+N more` counts the rest. The shape is at most 280 characters, so the note fits the run's 500-character note line with the in-front note (#195) after it. The diagnostic only reads, after the lookup already refused; what is clicked and what is matched don't change. A shape that can't be read leaves the refusal as it was, with `(its shape could not be read)`.

The report and the note never hold a name, a URL, an `href`, or the page's text. `tests/test_browser_safety.py` pins that the bubble check, and everything it reaches, reaches no input, no script, no navigation or scroll, no tab of its own, and no CDP sender.

### After the click: no reattach, no navigation

Once `click_message` returns, the run never calls `ensure_page`, `goto`, `new_page`, `scroll`, or `observe`, and never reattaches. The compose option's answer comes from the observation opened before the click. A lost tab or browser ends the run: `not_typed` before the first key is attempted, `unknown` after. A reopened tab would be a new page with no verified composer, and a reattach could land keys in a tab nobody checked.

### Typing into one verified composer

`BrowserRun.type_into_composer` types the rendered body into the composer the click opened. Before the first key, it verifies all of these, and types nothing if any fails. Every count includes hidden elements (`include_hidden=True`), so a minimized bubble counts.

**The composer wait.** The bubble and the compose option arrive a moment after the click. So before the first key, `type_into_composer` polls the full set of read-only checks below (the compose option seen, one composer, empty, the recipient, and focus) until they all pass in one pass, for at most `COMPOSER_WAIT_S` (5 seconds, a module constant pinned to its literal). The wait is read-only: it gives no input. A pass counts only when every check passes in it, including that the tab's URL is unchanged and that exactly one compose option was seen, with no later one; checks that passed in different passes don't add up. If no pass has succeeded by then, the prefill ends `not_typed`. The focus check is part of every authorizing pass. The wait first polls until one pass holds every check except focus; then, if the composer doesn't already hold focus, `_focus_seam` runs once, and never again; then the wait polls the full pass, focus included, for the rest of `COMPOSER_WAIT_S`. So the focus call still comes only after the recipient, emptiness and single-composer checks have passed, and before the full pass that authorizes the first key.

- **Exactly one composer is on the page.** The composer is found by role and name: role `textbox`, name `Write a message…` (with U+2026, the ellipsis character), exact, never by a CSS class or the `msg-form-…` id. It must sit in the bubble the recipient checks verified (see the next subsection). Any other composer on the page, such as a minimized bubble left from an earlier prefill or opened by the person, means more than one, and the prefill refuses with a reason that says another composer is on the page, in an open or minimized bubble, and asks the person to close the other bubbles. netkeeper never closes a bubble: closing one deletes its draft, and it's an input this ADR doesn't authorize.

  *Amended 2026-10-06 for #444:* decision 3 is also read **before** the Message click. `click_message` refuses, with no click, when any composer (role `textbox`, name `Write a message…`) or any `role="dialog"` named `Messaging` is already on the profile page, hidden ones counted: the same queries as the check after the click. The reason is "a message bubble is already open in Chrome, minimized ones included; close it, then try again". The run ends `not_typed` with no click attempted, so no second bubble opens and the tab isn't handed over. A leftover bubble for the same contact refuses too, so the person closes it before trying again. A read that fails refuses with its own reason, "whether a message bubble is open could not be read", also with no click. Other textboxes and dialogs (a comment box, Contact info) don't count. Before this, a leftover bubble cost a click and left a second, empty bubble. The check after the click stays, for a bubble that arrives with the click.
- **The composer is empty.** It reads as empty under [the text rule](#reading-the-composers-text). The capture shows that a minimized bubble keeps its draft across pages, so the Message click can restore a bubble that already holds a draft for this contact. The prefill refuses, and the person clears the draft.
- **The composer's recipient is this contact.** See the next subsection.
- **The verified composer holds focus.** Focus is read through Playwright's own selector engine (a `:focus` match on the composer's locator), never through `evaluate` or other script of netkeeper's in the page. If the composer doesn't hold focus after the one focus call [below](#focusing-the-composer), the prefill types nothing.
- **No chunk holds a control character.** The method refuses, before the first key, a plan in which any chunk contains `\n`, `\r`, or any other C0 or C1 control character. Playwright's `keyboard.type` maps `\n` and `\r` to the Enter key, so a newline is only ever a `newline=True` step of the plan, pressed as Shift+Enter. `TypeStep`'s constructor already refuses such a chunk; this check doesn't rely on it.

#### The recipient

The composer's `aria-label` doesn't name the recipient, so the recipient comes from the Message links, from what the page loaded, and from the bubble. All three must pass. A contact without a public id (slug) can't be prefilled at all: the run navigates to `/in/<slug>/` and the checks read it. A stale slug fails safe: the profile redirects or doesn't load, the tab isn't on the contact's path, and nothing is clicked.

1. **The Message links.** Every link named **Message** named the contact's profile URN and bare id (checked in `click_message`).
2. **What the page loaded.** From the observation opened before the click, the run reads the compose option, `GET voyagerMessagingDashComposeOptions/<fsd_composeOption urn>`:
   - the first part of the `fsd_composeOption` URN in the request's path is the contact's bare profile id;
   - the answer's `data.composeNavigationContext.recipientUrns` is exactly one URN, the contact's `urn:li:fsd_profile:<id>`;
   - `composeOptionType` is `REPLY` with an `existingConversationUrn`, or `CONNECTION_MESSAGE` without one. Any other type, a `REPLY` without a conversation, or a `CONNECTION_MESSAGE` with one is refused.

   No compose option seen, or more than one, means `not_typed`. A compose option that arrives later, after the one the checks read, refuses as `another_compose`: before the first key in the authorizing pass (`not_typed`), and after it in every per-key check (`partially_typed`). The type decides which bubble layout the next check expects.
3. **The bubble.** At most one `role="dialog"` named `Messaging` may be on the page in either layout. Then:
   - **`REPLY`, an existing conversation.** Exactly one `role="dialog"` named `Messaging` holds the composer. Its header `h2` holds exactly one link, and that link is `/in/<profile id>/`, where the id is the contact's bare profile id (the same id as in the URN, not the vanity slug). **The other layout** is any `New message` heading anywhere on the page; one refuses.
   - **`CONNECTION_MESSAGE`, never messaged.** This check doesn't rely on `role="dialog"`, which the capture didn't confirm. The scope is the innermost element that contains both the `New message` heading and the verified composer. P4-03 finds it from the composer upward, with an XPath ancestor such as `xpath=ancestor::*[.//h2[normalize-space()='New message']][1]` (the nearest ancestor that holds the heading), rather than `locator("*").filter(has=heading).filter(has=composer).last`, which tests every element on the page. Every `has=` filter and inner locator in these checks includes hidden elements. Exactly one `New message` heading is on the page. Inside the scope there's exactly one chip (a button whose name starts `Remove `), exactly one recipient field named `Enter message recipients`, and exactly one `/in/` link, the card's, whose slug is the contact's public id. Zero chips, two chips, two cards, or a card for anyone else is refused. Scoping matters: the profile page around the bubble links its own slug too, so a page-wide search would find the right slug whatever the bubble said. When the profile page had exactly one `h1`, the chip's name must also be `Remove ` followed by that `h1`'s name, LinkedIn's own name for the profile, never netkeeper's stored name. Both sides are accessible names, with whitespace collapsed to single spaces, trimmed, and normalized to NFC. Playwright has no getter for an accessible name, so each name is taken from three candidates: the element's `aria-label`, its rendered text, and its rendered text with the `aria-hidden` parts removed. One candidate must be confirmed by Playwright's exact role-and-name matcher (`get_by_role(..., name=..., exact=True)`) as naming that same element, run with `include_hidden=False` for a visible element; the confirmed candidate is the name. So pronouns in an `aria-hidden` span aren't part of it. A name that can't be read this way (for example, one given through `aria-labelledby`) refuses with its own reason, `recipient_name_unreadable`, distinct from a mismatch. When the read name and the matcher disagree on the chip, the prefill refuses with `recipient_name_mismatch`. When they disagree on the `h1`, or the `h1`'s name can't be read, the name check is skipped, as with zero or two `h1` elements. A skipped check writes one INFO log line in fixed words, with no name. `MessageOutcome` gains a defaulted field, `recipient_name_checked`: `True` when the chip's name was checked against the `h1`, `False` when that check was skipped, and `None` when no chip check applies (an existing conversation, or a refusal before the bubble). The run records it in its counts, so CP8 can see how often the chip check runs. A mismatch refuses with its own reason, `recipient_name_mismatch`, so CP8 can tell it from the other refusals. With zero `h1` elements, two or more, or an `h1` whose name the matcher doesn't confirm, this check is skipped, and the scoped card's slug still binds the recipient. The known risk: a badge, pronouns, or a former name inside the `h1` would make every prefill to that person refuse, and two refusals in a row park the enrollment. That's the safe direction; CP8 checks for it, and #429 tracks it. **The other layout** is a `role="dialog"` named `Messaging` that doesn't contain the `New message` heading; one refuses. A dialog that does contain it is this bubble's own root, so it never makes this layout refuse itself.

The bubble's DOM alone never authorizes typing, and neither does the compose answer alone. The draft's earlier rule ("no observed conversation means `not_typed`") is replaced: a contact you've never messaged has no conversation, and the compose option names the recipient either way.

For an existing conversation, the outcome's `conversation_urn` comes from the page's own `messengerMessages` thread request, observed through the observation opened before the click. Its `conversationUrn` variable is the `urn:li:msg_conversation:(…)` form that the inbox poll (#433) matches, and it's used only when its thread id matches `existingConversationUrn`'s. When that request isn't seen, or its thread id differs, `conversation_urn` is `None`. The fallback is never the `fsd_conversation` form from the compose option, which the poll can't match. A never-messaged contact has no conversation yet, so its `conversation_urn` is `None`.

#### Reading the composer's text

The composer is a `contenteditable` element, and its text is read with Playwright locator reads, never with script:

- Each paragraph (`p`) inside the composer is read with `inner_text`. A paragraph boundary is one newline, and a `<br>` inside a paragraph is one newline.
- A paragraph that holds only a `<br>` is an empty line, and a trailing `<br>` at the end of a paragraph adds nothing. So the empty composer the capture shows, `<p><br></p>`, reads as the empty string.
- A no-break space (U+00A0) reads as a space. A browser may write one for a typed space.
- Text outside a paragraph makes the composer unreadable: a bare text node directly in the composer, or text before a `<p>`. The check is that the composer's whole `text_content()` equals its paragraphs' `text_content()` joined with nothing between them. In real Chrome, selecting all, pressing Backspace, and typing leaves bare text, which the paragraph rule alone would read as empty. Whitespace-only text between paragraphs counts as text outside a paragraph too, which fails safe.
- Anything else in the composer the rule can't read this way, such as an element other than `p` and `br`, also makes the text unreadable. Unreadable text fails the check: before the first key the prefill refuses (`not_typed`), and after it typing stops (`partially_typed`). If LinkedIn renders an inserted emoji as an `<img>`, the composer becomes unreadable after that emoji and the run ends `partially_typed`. That's fail-safe. CP8 watches for it, and for whitespace-only text between paragraphs, either of which would stop prefills that should succeed.

The expected text is the typed prefix with each newline step as `\n`, with the same mapping applied: a no-break space in the body becomes a space before the two are compared. P4-03's smoke replica uses a real `contenteditable`, so the rule is tested against a browser's own editing, not a fake.

#### The replay

Typing replays the plan: lognormal delays, median 140 ms a character, no typos, a 300-second ceiling. Each step runs in this order: **the delay, then the checks, then the key**, with nothing awaited between the checks and the key.

- A space is sent with `keyboard.insert_text`, never `keyboard.type`. Playwright's `keyboard.type` maps a space to the Space key, which activates a focused button. So the only key that could activate a focused button is Shift+Enter.
- Any other single printable ASCII character is typed with `keyboard.type`.
- Any other chunk (`TypeStep.needs_insert_text`: an accented letter, an emoji sequence) is sent with `keyboard.insert_text`.
- A `newline=True` step is pressed as Shift+Enter.

The run checks for cancel at each step's checks, and stops when the tab closes or its URL changes.

**The point of no return** is the first key call attempted: the first `type`, `insert_text`, or `press`, whether or not it returned. From then on, any stop, including an exception from a check's read, ends the run as `partially_typed` (a check failed cleanly) or `unknown` (the run can't tell what the composer holds), never `not_typed`. Before that point, every refusal and every exception is `not_typed`.

*Amended 2026-10-10 for #481.* On CP8b (#479), the click opened the **New message** bubble with the right contact's chip and card, and the check refused it with "the new-message bubble is for someone else", twice in a row. The rule above wanted exactly one `/in/` link in the scope, by slug. LinkedIn's card most likely links the photo and the name, which is two links to the same person, and may link by member id instead of slug. So the card check is now (`read_card_link`):

- It reads every element in the scope that has an `href`, hidden ones included, whatever its role (`[href]`), and never resolves an `href` against the page.
- Ignored, as naming no page: an empty `href`, a fragment-only one (`#…`), and one whose scheme isn't `http` or `https` (`javascript:`, `mailto:`).
- **The contact**: on `https://www.linkedin.com`, or root-relative, a path whose raw `/` segments are `in` (decoded, case-folded), then the contact's slug (decoded, case-folded) or member id (decoded, exact, as the existing conversation's header link is compared), then any number of further segments (a photo overlay, for example). Each further segment is non-empty (one trailing empty one is allowed), holds no `%25`, and, decoded, holds no `/` or `\` and isn't `.` or `..`. The query and fragment are ignored.
- **Someone else**: every other `/in/` link (another slug or id, the id in another case, another host, `http`, an empty or bad segment), and any `href` that has a backslash (raw or `%5C`), is path-relative (no host and a path not starting `/`, with a scheme or without: Chrome resolves `https:bob/` against the page), has a `.` or `..` segment once decoded (`%2e` and `%2F` included), has an empty segment before the last (`//in/…`), or has `;` parameters on a person route's first segment (`/in;x/…`). LinkedIn's other person routes count too (`/mwlite/`, `/pub/`, `/profile/view?id=`, `/sales/lead/`, `/sales/people/`, `/talent/profile/`, `/recruiter/`), as does a query or fragment, decoded twice, naming a member by `urn:li:fsd_profile:`, `urn:li:fs_miniProfile:`, or `urn:li:member:`, unless the token is exactly the contact's member id. A route with the contact's own id is ignored: only an `/in/` link confirms the contact.
- At least one link must be the contact, and none may be anyone else. Each refusal has its own reason: `the new-message bubble links to no profile`, `the new-message bubble links to more than one person` (the contact and anyone else, or two others), and `the new-message bubble is for someone else` (one other person only). Other people are told apart by their token alone, so a relative and an absolute link to the same person are one person.
- A refusal writes one INFO line in fixed words: the number of profile links, and for each one its form (`slug` or `id`), any notes (`more path`, `another host`, `query member`, `other route`, `unsafe path`), and whether it matches. It never writes an `href`, a slug, an id, or a name. The check runs on every poll of the composer wait and before every key, so the line is written only when it differs from the run's last one. A card that passes is logged once per run in the same words, so the next checkpoint shows the live card's shape.

The same check runs before every key and before auto-send's Send (ADR 0008), so all three accept and refuse the same cards. The chip count (exactly one), the recipient field, the heading, the scope, and the chip's name check are unchanged.

The #479 report also showed that LinkedIn's profile page has **no `h1`** now, so the chip's name check is skipped (the INFO line above says so) and the top card can't be found. No replacement name source is reliable without a capture of the new page: a heading found by role could be some other heading, and netkeeper's stored name can differ from LinkedIn's (a former name, pronouns, an emoji), and either would refuse the right person. Two refusals park the enrollment. So the check stays skipped, and the card's link identity binds the recipient, as it did with zero `h1` elements before. #429 tracks the capture that would settle it.

### Checked before every key

The checks above don't run only once. Playwright sends keys to whatever has focus and doesn't check where that is. If focus moved mid-type, to the Send button for example, a Shift+Enter could send the message, and the rest of the body could land in the wrong place. In the never-messaged layout, focus could also move to the recipient field, and the rest of the body would become a people search.

- Right before every chunk and every Shift+Enter, after the step's delay, `type_into_composer` checks again that exactly one composer is on the page, that the bubble's recipient is unchanged (the dialog's header link, or the one chip and its card's link in the scope), that the tab's URL is unchanged, that no later compose option has arrived, that the composer's text equals the prefix typed so far, and, last, that the verified composer holds focus. The focus check is the last read before the key, so the window between it and the key is as short as it can be.
- Any failed check stops typing at once, with the outcome `partially_typed`.
- Before the hand-over, the composer's text must equal the body, or the outcome is `partially_typed`.
- The prefill UI tells the person not to type or click in Chrome while the prefill types.

### Focusing the composer

Where focus lands after the Message click is unknown (#429, item 5), so netkeeper may focus the composer once. The maintainer chose this on 2026-10-06 (decision 5, option B, the safety review's recommendation).

`type_into_composer` may call `BrowserRun._focus_seam`, which calls `focus()` on the verified composer's locator, never a click. It's called at most once per run, only after every recipient, emptiness, and single-composer check has passed, and only when the composer doesn't already hold focus. Focus is then checked again, and if the composer still doesn't hold it, the prefill ends `not_typed`. `ALLOWED_INPUTS` names `focus` at `_focus_seam`, which only `type_into_composer` calls.

The alternative, no focus input at all, was rejected: if focus doesn't land in the composer after the click, every prefill would refuse until an amendment.

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

Once the Message click has been attempted, the run's tab is never closed: not by `close()`, not on an error, and not when the run is cancelled (including `asyncio.CancelledError`). At the end of a prefill run (spec 11.6), `BrowserRun.hand_over()` drops the run's reference to its tab and detaches without closing it, so the provider's later `close()` closes nothing. It changes no focus (see [Bringing the tab to the front](#bringing-the-tab-to-the-front)). From then on the tab isn't netkeeper's: no later run reuses it, navigates it, or closes it. `hand_over` is a new ending, distinct from `close()`, and is reached only from `netkeeper/linkedin/page_messaging.py`. The run releases the browser lock after typing; it doesn't wait for the send.

Every run that attempted the Message click hands its tab over, whatever the outcome. A click that raised may still have opened a bubble, so an attempted click counts as a click:

- **After typing,** the bubble holds the body or part of it. The capture shows it keeps its draft if the person minimizes it or moves to another page.
- **After a `not_typed` refusal that followed the click,** the bubble is open and empty. Closing it would be safe, since it holds no draft, but it's an input this ADR doesn't authorize, and closing the tab may not clear it, because a bubble persists across pages. So the tab is handed over like any other, and the UI shows the refusal's reason and asks the person to close the empty bubble. There's no option to close the tab instead: once the click has been attempted, `close()` never closes it, so bringing that option back would mean changing `close()` too.

Because the next prefill refuses a page with more than one composer, the person closes that bubble (after sending, or to discard the draft) before the next prefill. P4-09 allows one open prefill at a time.

**Confirming the send.** The next inbox poll (P4-02, #381), or the "I sent it, check now" action, confirms the send by matching the first outbound message in the conversation dated after `prefilled_at`. So `prefilled_at` must be a time before the first key: the moment typing starts, taken before the first key call (#416's review, #382's comments). P4-09's `record_prefill_outcome` gets an explicit `prefilled_at=` argument, which P4-03 adds and passes, instead of reusing the `now` it records the outcome at. P4-03 also corrects `CONFIRM_SKEW`'s docstring in `netkeeper/services/campaign_replies.py` (about line 186), which says `prefilled_at` is taken when the outcome is recorded. If `prefilled_at` were later than the person's click on Send, or the clocks disagreed by a few seconds, the send would never be confirmed.

After a failure mid-type, the tab still holds what was typed:

- The UI flags `partially_typed` and `unknown` with "Part of a message is in the composer and may be kept as a draft. Clear it, or close the message bubble, which deletes the draft."
- netkeeper never clears it. Clearing is an input this ADR doesn't authorize.
- If the run crashes, the message stays claimed, and P4-09 lists it as interrupted under "waiting for you", holding the one open slot until the person discards it. It's never claimed or typed again.

### The pins

P4-03 (#382) changes `tests/test_browser_safety.py` in the same pull request as the code these pins guard, not in this one.

#444 adds, in the same pull request as its code:

- `ALLOWED_CONTEXT_MUTATIONS` allows `new_cdp_session` in `BrowserRun._read_click_geometry`, and `CDP_SENDERS` lets that function send exactly `Page.getLayoutMetrics` (no params), `DOM.getDocument` (`depth`), `DOM.querySelectorAll` (`nodeId`, `selector`), `DOM.describeNode` (`nodeId`, `depth`), `DOM.getBoxModel` and `DOM.getContentQuads` (`nodeId`), and `DOM.getNodeForLocation` (`x`, `y`, `ignorePointerEventsNone`); neither CDP site may send the other's methods.

#490 adds `DOMSnapshot.captureSnapshot` (`computedStyles`) to `READ_ONLY_CDP_METHODS`, sent only from `BrowserRun._pseudo_hosts`, which the click's geometry read and the Message check share.
- The click's candidates are drawn only from the href-bound locator (`visible.nth(index)`, and `top.first` from `bound.and_(after_heading)`).
- `MESSAGE_TOP_CARD`, `MESSAGE_MAX_CANDIDATES`, `HIT_TOLERANCE_PX`, the refusal's words, and the failure and target categories are pinned to literals.
- Runtime: a sticky copy off screen is passed over for the top card; a covered top card gives way to a clear copy; everything covered refuses with no click; an overlay with `pointer-events: none` doesn't cover; a geometry read that fails clicks the top card, unchecked; a click that raises records its category and logs no page text; a geometry read that hangs times out and clicks unchecked; an open or minimized bubble already on the page refuses before the click. The smoke replica reproduces the sticky (a copy wholly above the viewport, on which the old first-visible choice fails as outside the viewport), covered, sidebar, minimized-bubble, and leftover-bubble pages, and reads each category from Playwright's own error.

Static pins:

- `ALLOWED_INPUTS` entries for exactly three methods: `click` in `BrowserRun.click_message`; and `keyboard` (read once, into a local), `type`, `insert_text`, and `press` in `BrowserRun.type_into_composer`. Also `focus` in `BrowserRun._focus_seam`, and a static pin that `_focus_seam` has exactly one call site, in `type_into_composer`.
- A literal check that the only `press` argument anywhere is `"Shift+Enter"`.
- No `keyboard.down` and no `keyboard.up` anywhere. No `focus()` anywhere, except the one call in `_focus_seam`.
- `keyboard.type` is never called with a space, or with a chunk that isn't one printable ASCII character other than a space.
- The Message link's role and name (`"link"`, `"Message"`), the composer's role and name (`"textbox"`, `"Write a message…"`), the dialog's name (`"Messaging"`), the `"New message"` heading, the `"Enter message recipients"` field, the `"Remove "` chip prefix, `COMPOSER_WAIT_S` (`5`), and the claim lapse (`60` seconds) are module constants pinned to literals.
- No string argument to a locator-building call (`get_by_role`, `get_by_text`, `get_by_label`, `get_by_title`, `locator`, `filter`, or a `has_text` or `name` keyword) under `netkeeper/linkedin/` matches "send" or "submit", ignoring case.
- The `click` in `click_message` is on a locator built with the contact's `href`, never on a bare `nth` of the role locator.
- No code under `netkeeper/` passes `allow_newlines=` to `typing_plan`.
- `pacing.SHIFT_ENTER_NEWLINES_ALLOWED` is true only together with the Shift+Enter press pin.
- `bring_to_front` is called at one site, inside the prefill's start, before `click_message`; a runtime pin checks that it's called once per run.
- `netkeeper/linkedin/page_messaging.py` in `BROWSER_MODULES` and `BROWSER_CALLERS`.
- `hand_over` reachable only from `page_messaging.py`.
- From the `click_message` call on, `PagePrefill.prefill` and the methods it calls reach no `goto`, `reload`, `go_back`, `go_forward`, `new_page`, `ensure_page`, reopen, `scroll`, or `observe`; neither does `BrowserRun.hand_over` or anything it calls (#456).

Runtime pins, against a fake page:

- A plan with a `"\n"` chunk gives `not_typed` with zero keys.
- A `typing_plan` failure, including an unexpected exception, gives `too_long` or `not_typed` with zero keys, no navigation, and no budget spent.
- A busy browser lock gives `not_typed` with zero navigation and zero budget spent.
- A claim older than 60 seconds (by `Message.scheduled_at`) gives `not_typed` with zero navigation and zero budget spent.
- Three Message links with the contact's `href` give one click, on the first visible one, through a locator that matches the `href`.
- A link named Message that names another profile, mismatched `profileUrn` and `recipient`, a repeated query parameter, no link, or no visible link gives `not_typed` with no click.
- A `button` named Message beside the links is ignored: the prefill proceeds and never clicks it.
- A brief scroll that leaves the top card's control under a sticky header that appears on scroll gives one scroll back up before the click, then one click on the top card's control at the page's top. A page that stays covered gives `not_typed` with no click and a log line with the category only. The sticky header's own button is never clicked, and an auto-send scrolls back the same way and sends once (#470).
- The compose-option and `messengerMessages` observations are opened before the click; after the click, no `observe`, `scroll`, `ensure_page`, `new_page`, or `goto` is called.
- A compose option whose path id or `recipientUrns` isn't the contact's, with more than one recipient, with an unknown `composeOptionType`, or missing, gives `not_typed`.
- A `REPLY` bubble whose header `h2` links to another profile id, or holds two links, gives `not_typed`. A `REPLY` page with any `New message` heading gives `not_typed`.
- A `CONNECTION_MESSAGE` page with a `Messaging` dialog that doesn't contain the `New message` heading gives `not_typed`. One whose bubble root is a `Messaging` dialog containing the heading proceeds.
- Two `Messaging` dialogs, including a hidden or minimized one, give `not_typed` in either layout.
- A never-messaged bubble with zero chips, two chips, two `New message` headings, two `/in/` links in the scope, or a card for another slug gives `not_typed`, with or without a `role="dialog"` root. A page whose profile links the contact's slug while the bubble's card links another slug gives `not_typed`. (Amended for #481: two links to the contact, by slug or by id, and a longer path under the contact's profile, proceed. A link to the contact beside a link to anyone else, a card linking only someone else, a member id in another case, the contact's slug on another host, an unsafe or path-relative `href`, another person route, a query naming another member, an `href` on an element of any role, and no profile link each give `not_typed` with their own reason, before typing, mid-type, and before Send.)
- A chip whose name doesn't match the profile's `h1` gives `not_typed` with the reason `recipient_name_mismatch`; the comparison collapses whitespace and normalizes to NFC. With zero or two `h1` elements, or an `h1` name the matcher doesn't confirm, the check is skipped and the card still decides. A chip name the matcher doesn't confirm gives `recipient_name_mismatch`; a chip name given through `aria-labelledby` gives `recipient_name_unreadable`; `aria-hidden` text in the `h1` or the chip isn't part of the name. A skipped `h1` check logs one fixed-words INFO line with no name and records `recipient_name_checked=False`; a checked one records `True`; an existing conversation records `None`. A name confirmed only through the candidate with its `aria-hidden` parts removed passes.
- A later compose option gives `another_compose`: `not_typed` in the authorizing pass, `partially_typed` with zero further keys mid-type.
- A second composer on the page before the first key, including a hidden one, gives `not_typed`; one appearing mid-type stops typing.
- A non-empty composer gives `not_typed`; `<p><br></p>` reads as empty; a no-break space reads as a space, in the read and in the expected text.
- A bare text node in the composer, or text before a `<p>`, makes the composer unreadable: `not_typed` before the first key, `partially_typed` after.
- Checks that haven't all passed after `COMPOSER_WAIT_S` give `not_typed` with zero keys; checks that pass during the wait proceed, and the wait gives no input.
- An existing conversation's `conversation_urn` is the `urn:li:msg_conversation:(…)` URN from the observed `messengerMessages` request; with no such request, or a different thread id, it's `None`, never the `fsd_conversation` form.
- Focus not in the composer before the first key gives `not_typed`; focus moving away after chunk k gives zero further keys. `_focus_seam` is called at most once, only after a pass of every check except focus, and the full pass, focus included, is polled after it.
- A space is sent with `insert_text`, never with `keyboard.type`.
- No delay is awaited between a step's checks and its key, and the focus check is the last read before each key.
- A URL change before the first key gives `not_typed`; a URL change mid-type gives `partially_typed` with zero further keys.
- A cancel mid-type gives `partially_typed` with zero further keys.
- A cancellation, or a call to `close()`, at any point after the Message click was attempted leaves the tab open.
- An exception from a check's read after the first key call was attempted gives `partially_typed` or `unknown`, never `not_typed`.
- Composer text that diverges from the typed prefix stops typing.
- A recipient (header link, or chip and card) that changes mid-type stops typing.
- A final composer text that differs from the body gives `partially_typed`, never `prefilled`.
- A `prefilled` outcome records a `prefilled_at` no later than the first key call.
- A `not_typed` refusal after the click hands the tab over and leaves the bubble open.
- A Message click that raises hands the tab over.
- After `hand_over()`, the provider's exit leaves the page open.
- After an attempted click, landed or not, and after `hand_over()`, `goto` and `ensure_page` raise, and the tab stays on the profile (#456).

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
- netkeeper focuses the composer at most once, after every recipient check, if focus didn't land there after the click. It never clicks into the composer.
- Handed-over tabs accumulate in the user's Chrome until the user closes them. That's deliberate: netkeeper closing a tab is how a draft would be lost.
- The person must leave Chrome alone while the prefill types. Touching it stops the prefill as `partially_typed`, which is the safe direction.
- If LinkedIn changes the Message control, the compose option, the composer, or how the bubble shows its recipient, the prefill refuses and types nothing. That's the intended failure.
- If LinkedIn changes what Shift+Enter does, nothing in this ADR detects it at run time. The capture is the evidence, and a later capture that contradicts it means newlines are refused (the flag goes back to false) until a new amendment.
- A future contributor keeps these true: the only clicks are Contact info and Message, each once, each bound to the profile it's on; the only typing is aimed at one verified composer for the contact, with focus and the composer checked again right before every key; the only key pressed is Shift+Enter; no code path presses Enter or clicks Send; and a handed-over tab is never touched again.

## Decisions recorded at acceptance

The maintainer accepted these on 2026-10-06:

1. **Where the tab comes to the front.** Once, at the run's start, before the Message click; `hand_over()` changes no focus. That adjusts the "brought to the front" wording of 2026-10-03.
2. **The claim lapse.** 60 seconds after `Message.scheduled_at`.
3. **Other open bubbles.** The prefill refuses a page that shows any composer besides the one the click opened, hidden ones included, so the person closes earlier bubbles, including the last prefill's, before the next prefill. (Amended for #444: also read before the click, which then isn't made.)
4. **The Message click rule.** Every link named Message must name the contact's URN and bare id, and the click goes through the first visible one, through a locator bound to that `href`. It replaces the draft's "exactly one, in the top card" rule. (Amended for #444: which visible one is set by [Choosing the control](#choosing-the-control). The rule that every link names the contact is unchanged.)
5. **Focusing the composer.** One `Locator.focus()`, in `_focus_seam`, after every check (option B), as [described above](#focusing-the-composer).

The typing indicator, a decision in the draft, was settled on 2026-10-05: accepted, with a possible toggle later (#430).
