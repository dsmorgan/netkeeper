# LinkedIn messaging shapes

This note records the structure of what LinkedIn's messaging pages load and which controls they show, as the maintainer's capture of **2026-10-05** showed it (#374; 2026-10-06 UTC). It records **structure only**: paths, query names, keys, `_type` names, enum values, how identifiers nest, counts, and the roles and accessible names of controls. It holds no name, slug, profile or member id, URN value, message text, token, or header value. The capture stays in the maintainer's private folder; nothing in this repository was copied from it.

[ADR 0006](adr/0006-observe-dont-request.md) records why netkeeper reads these answers rather than requesting anything itself. The hand-built fixtures that follow these shapes, with invented people, are `tests/messaging_pages.py`; `tests/test_messaging_pages.py` checks that none of their values appears in the capture. That check is opt-in, so no ordinary test run reads the private folder: it runs only with `NETKEEPER_CAPTURE_DIR` set to the capture folder (`NETKEEPER_CAPTURE_DIR=~/code/netkeeper-private/messaging-capture .venv/bin/python -m pytest tests/test_messaging_pages.py`), and skips otherwise. Only the maintainer, or an analysis session he approves, sets it. The inbox poll (P4-01, #380) and the prefill (P4-03, #382) are built from this note. Where the capture was silent, this note says so, and [What P4 reads, and what it assumes](#what-p4-reads-and-what-it-assumes) lists what the first supervised runs must confirm.

The capture holds five HAR files and four HTML files: the inbox, older conversations, one thread and a profile; opening the bubble from a profile and typing; the **Message** click for a connection never messaged; one real send; the **Message** control; and both kinds of bubble.

## Two clients on one page

The messaging pages are LinkedIn's older client, not the `flagship-web` client of [the connections and profile shapes](linkedin-flagship-web-shapes.md):

- `GET /messaging/` answers an HTML document (about 1.3 MB) with Ember markup and `<code>` bootstrap blocks. None of those blocks holds a conversation: the list arrives only by `fetch` after the page loads.
- The bubble that opens over a profile is Ember markup too (`msg-` and `artdeco` class names, `ember<n>` ids).
- The profile it opens over was `flagship-web` (elements carry `componentkey`), and the **Message** control on it is a `flagship-web` element.

So P4-01 reads Voyager GraphQL JSON, not flight, and P4-03 finds a `flagship-web` control and then an Ember composer.

## The requests

Every list and thread answer comes from one path. The `queryId` is `<name>.<hash>`; the hash differs for each variant of a query (4 hashes for `messengerConversations`, 3 for `messengerMessages`) and changes with LinkedIn's releases, so a reader matches the **name** and the field under `data` that names the variant, never the hash. `variables` is Rest.li syntax, `(key:value,...)`, with each URN percent-encoded inside it (`urn%3Ali%3Afsd_profile%3A<id>`; a compound URN's parentheses and comma are encoded too).

| What | Method and path | `queryId` name | `variables` keys | Answer field under `data` |
|---|---|---|---|---|
| The list, on load | `GET /voyager/api/voyagerMessagingGraphQL/graphql` | `messengerConversations` | `mailboxUrn` | `messengerConversationsBySyncToken` |
| The list, refreshed | same | `messengerConversations` | `mailboxUrn`, `syncToken` | `messengerConversationsBySyncToken` |
| Older conversations, first page | same | `messengerConversations` | `query:(predicateUnions:List((conversationCategoryPredicate:(category:PRIMARY_INBOX))))`, `count:20`, `mailboxUrn`, `lastUpdatedBefore` (epoch ms) | `messengerConversationsByCategoryQuery` |
| Older conversations, later pages | same | `messengerConversations` | the same `query`, `count:20`, `mailboxUrn`, `nextCursor` | `messengerConversationsByCategoryQuery` |
| One conversation by id | same | `messengerConversations` | `conversationIds:List(<msg_conversation urn>)`, `count:1` | `messengerConversationsByIds` |
| A recipient's conversation | same | `messengerConversations` | `mailboxUrn`, `recipients:List(<fsd_profile urn>)` | `messengerConversationsByRecipients` |
| A thread, on open | same | `messengerMessages` | `conversationUrn` (later: and `syncToken`) | `messengerMessagesBySyncToken` |
| A thread, scrolled up | same | `messengerMessages` | `deliveredAt` (epoch ms), `conversationUrn`, `countBefore:20`, `countAfter:0` | `messengerMessagesByAnchorTimestamp` |
| Several threads | same | `messengerMessages` | `criteria:List((conversationUrn,syncToken))` | `messengerMessagesBySyncTokensInBatch` |
| Counts, receipts, quick replies | same | `messengerMailboxCounts`, `messengerSeenReceipts`, `messengerQuickReplies` | `mailboxUrn` or `conversationUrn` | not read |

The inbox capture had 8 list requests, 9 thread requests, and 3 counts requests, each answered `200` with a whole body; none was aborted. They are `fetch` requests answered `application/graphql`.

Around them the page also sends `POST /voyager/api/voyagerMessagingDashMessagingBadge?action=markAllMessagesAsSeen`, `POST .../voyagerMessagingDashMessengerMessageDeliveryAcknowledgements?action=sendDeliveryAcknowledgement`, `POST /voyager/api/messaging/dash/presenceStatuses`, `GET .../voyagerMessagingDashConversationNudges`, `GET .../voyagerMessagingDashSecondaryInbox?q=previewBanner`, and `/realtime/` traffic (below). Opening the inbox marks messages as seen; that's the page's own doing, and a poll can't avoid it.

`/voyager/api/messaging/conversations`, the path `netkeeper/linkedin/voyager.py` targets, appears nowhere in the capture.

## The envelope

A GraphQL answer is plain JSON, **not** the normalized `data`/`included` form and not flight:

```
{"data": {"_type": "<recipe hash>", "_recipeType": "<recipe hash>",
          "<answer field>": {"_type": "com.linkedin.restli.common.CollectionResponse",
                             "_recipeType": "...", "elements": [...], "metadata": {...}}}}
```

There is no `included` key; every conversation, message and participant is inline. `_recipeType` values, and the top-level `_type`, are hashes (`com.linkedin.<32 hex>`) that a reader ignores. The `_type` values that name things are stable names such as `com.linkedin.messenger.Conversation`.

| Answer field | `metadata` `_type` | `metadata` keys |
|---|---|---|
| `messengerConversationsBySyncToken` | `com.linkedin.messenger.SyncMetadata` | `newSyncToken` (on load); a refresh adds `deletedUrns` and `shouldClearCache` |
| `messengerConversationsByCategoryQuery` | `com.linkedin.messenger.ConversationCursorMetadata` | `nextCursor` (a string) |
| `messengerConversationsByRecipients` | none | the collection has no `metadata` |
| `messengerConversationsByIds` | none | the field is a bare **list** of conversations, not a collection |
| `messengerMessagesBySyncToken` | `com.linkedin.messenger.MessageMetadata` | `newSyncToken`, `deletedUrns`, `shouldClearCache` |
| `messengerMessagesByAnchorTimestamp` | `com.linkedin.messenger.MessageMetadata` | `prevCursor`, `nextCursor` |
| `messengerMessagesBySyncTokensInBatch` | none | a list of collections, each with `elements` and `metadata` |

The compose requests below are Voyager's normalized form instead: `application/vnd.linkedin.normalized+json+2.1`, `{"data": {...}, "included": []}`, with `$type` keys rather than `_type`.

## Where each URN sits

`<thread id>` is `2-` followed by base64 of `<uuid>_<3 digits>`. `<message id>` is `2-` followed by base64 of `<13-digit ms>b<5 digits>-<3 digits>&<the thread's base64-decoded part>`. `<profile id>` is the 39-character `ACo…` id, as in the flagship-web note.

| What | Key | Shape |
|---|---|---|
| A conversation | `entityUrn` | `urn:li:msg_conversation:(urn:li:fsd_profile:<mailbox owner's id>,<thread id>)` |
| The same, older form | `backendUrn` | `urn:li:messagingThread:<thread id>` |
| Its page | `conversationUrl` | `https://www.linkedin.com/messaging/thread/<thread id>/` |
| A participant | `conversationParticipants[].entityUrn` | `urn:li:msg_messagingParticipant:urn:li:fsd_profile:<id>` |
| A participant's profile | `conversationParticipants[].hostIdentityUrn` | `urn:li:fsd_profile:<id>` (a company: `urn:li:fsd_company:<n>`) |
| A participant's member id | `conversationParticipants[].backendUrn` | `urn:li:member:<n>` (a company: `urn:li:company:<n>`) |
| A participant's profile url | `...participantType.member.profileUrl` | `https://www.linkedin.com/in/<profile id>`: by id, **not** by vanity slug |
| A message | `entityUrn` | `urn:li:msg_message:(urn:li:fsd_profile:<mailbox owner's id>,<message id>)` |
| The same, older form | `backendUrn` | `urn:li:messagingMessage:<message id>` |
| A message's conversation | `conversation.entityUrn`, `backendConversationUrn` | the two conversation forms above |
| A message's sender | `sender.hostIdentityUrn` (and `sender.entityUrn`) | `urn:li:fsd_profile:<id>` |
| A message's time | `deliveredAt` | epoch milliseconds, 13 digits |
| A message's text | `body.text` | a string; a multi-line message holds `\n`, and `body.attributes` may mark `lineBreak`, `paragraph`, `bold`, `hyperlink`, `list` ranges |
| The compose option | path segment | `urn:li:fsd_composeOption:(<recipient's bare profile id>,NON_SELF_PROFILE_VIEW,<24-character token>)` |
| An existing conversation, from the compose option | `composeNavigationContext.existingConversationUrn` | `urn:li:fsd_conversation:<thread id>` (the same thread id, without the mailbox) |

A conversation's participants include the mailbox owner: in all 122 captured list items, exactly one participant had `participantType.member.distance` `SELF`, and its `hostIdentityUrn` was the owner in the conversation URN. The others carry `DISTANCE_1`, `DISTANCE_2` or `DISTANCE_3`. **The counterpart is the participant that isn't the owner**; the mailbox owner is the first part of every conversation and message URN, and the `mailboxUrn` the page sends.

A message's `actor` and `sender` named the same participant in every captured message that had both. **Two list messages had `actor: null`** with a `sender` and a body, in ordinary conversations; read `sender`, never `actor`.

The owner's own messages carry `originToken`, a 36-character uuid; everyone else's carry `originToken: null`. In the capture this held for all 50 outbound and 70 inbound list messages and all 19 thread messages, so `outbound` is `sender.hostIdentityUrn == mailbox owner`, and `originToken` agrees with it.

## A conversation, and its kinds

Each list item is a `com.linkedin.messenger.Conversation` with these keys: `entityUrn`, `backendUrn`, `conversationUrl`, `categories`, `groupChat`, `state`, `title`, `conversationTypeText`, `conversationVerificationLabel`, `conversationVerificationExplanation`, `headlineText`, `shortHeadlineText`, `descriptionText`, `contentMetadata`, `conversationParticipants`, `creator`, `createdAt`, `lastActivityAt`, `lastReadAt`, `read`, `unreadCount`, `notificationStatus` (`ACTIVE`), `disabledFeatures`, `hostConversationActions`, `incompleteRetriableData`, and `messages`. The category answer adds `draftMessages` (an empty collection in all 60 items). Two items had no `messages` key at all.

The list is ordered by `lastActivityAt`, newest first, in every captured answer.

**How kinds are marked.** The capture had 122 list items: every one had `groupChat: false` and exactly two participants. Four category sets appeared:

| `categories` | Count | What else marks them |
|---|---|---|
| `INBOX`, `PRIMARY_INBOX` | 58 | Ordinary one-to-one conversations. `state` `null`. One message had a `file` render item |
| `INBOX`, `PRIMARY_INBOX`, `INMAIL` | 40 | Conversations that began as InMail. `state` `PENDING`, `ACCEPTED` or `DECLINED`; often a `subject` on the message; 14 pending ones had the `InMail` label (below) and a `hostUrnData` render item (`type` `SALES_INMAIL` or `PREMIUM_INMAIL`); 5 were sponsored, with the `Sponsored` label and a `messageAdRenderContent` item |
| `INBOX`, `SECONDARY_INBOX`, `INMAIL` | 3 | Pending requests, with `hostUrnData` |
| `ARCHIVE`, `INMAIL` | 21 | Sponsored messages (18) and `LinkedIn Offer` items (3) |

- **Sponsored.** 23 items had `conversationTypeText.text` `Sponsored`, and 3 `LinkedIn Offer`; 14 had `InMail`. These are LinkedIn's own labels. A sponsored item also carries a `messageAdRenderContent` render item (`status`, `sponsoredCampaignUrn`, `subContent`, and tracking keys), or a `conversationAdsMessageContent` render item with `contentMetadata.conversationAdContent`. Six items had an **organization** participant: `participantType.organization` (`com.linkedin.messenger.OrganizationParticipantInfo`), `hostIdentityUrn` `urn:li:fsd_company:<n>`, `participantType.member: null`.
- **InMail.** `INMAIL` in `categories`. It stays after the person accepts, so an accepted InMail with a contact sits in `PRIMARY_INBOX` beside ordinary conversations.
- **A group.** `groupChat: true`, more than two participants, and probably a `title` (7 one-to-one items had a `title` too). **No group was in the capture**; the fixture is invented from these key names.
- **A system message.** **Not seen.** The nearest thing was the two `actor: null` messages.

`disabledFeatures[].disabledFeature` lists features off for a conversation (`ADD_PARTICIPANT`, `CREATE_GROUP_CHAT_LINK`, `REPLY`, and so on); on a one-to-one conversation `ADD_PARTICIPANT` and `REMOVE_PARTICIPANT` were always present.

## The list carries only the last message

Every list item held **at most one message**: 120 held exactly one, and 2 had no `messages` key. That message's `deliveredAt` equalled the item's `lastActivityAt` in all 120. Earlier messages come only from `messengerMessages`.

So under the 2026-10-03 decision, the poll opens threads **by navigation only** (to `conversationUrl`), only for conversations in `InboxJobSpec.open_threads_for` (a prefilled message or a live enrollment), and **at most 5 per poll** (`netkeeper/linkedin/inbox.py`, `MAX_THREADS_OPENED`). A reply that arrived after another newer message, in a conversation the poll doesn't open, is seen only as the last message.

## A thread

Opening a thread sends `messengerMessages` with `conversationUrn`, answered by `messengerMessagesBySyncToken`. The captured answers held 1 to 5 messages, **newest first**. Each is a `com.linkedin.messenger.Message` with the keys `entityUrn`, `backendUrn`, `backendConversationUrn`, `conversation`, `body`, `subject`, `deliveredAt`, `actor`, `sender`, `originToken`, `messageBodyRenderFormat` (`DEFAULT`, or `EDITED` for an edited message), `renderContent`, `renderContentFallbackText`, `reactionSummaries`, `footer`, `inlineWarning`, and `incompleteRetriableData`: the same shape as the list's last message.

Scrolling up sent `messengerMessagesByAnchorTimestamp` with `countBefore:20`; both captured answers were empty, since the thread was short. The order of a non-empty answer is assumed to match the sync answer's.

## The Message control

On a profile, **Message** is an `<a>` with no `aria-label`; its accessible name is its text, `Message`, beside an icon (`aria-hidden`). Its `href` is

```
/messaging/compose/?profileUrn=urn:li:fsd_profile:<id>&recipient=<id>&screenContext=NON_SELF_PROFILE_VIEW&interop=msgOverlay
```

where `recipient` is the same bare id as in `profileUrn`. The server-rendered profile writes the `href` relative; the copy taken from the live page (`message-control-2.html`) had it absolute, `https://www.linkedin.com/messaging/compose/?…`. The `<a>` carries `aria-disabled="false"` and a `componentkey`. A sibling `<button type="button" aria-expanded="false">` whose text is **More** holds the overflow menu.

**There is more than one Message control.** The captured profile document rendered **three** `<a>` elements whose text is exactly `Message`, each with its own `componentkey`, all with the same compose `href` naming the profile's own id. Its flight data named the compose url six more times, and one lazy card (`actions/component`) once more, all for the same recipient. Which of the three a person sees (the top card, the sticky header that appears on scroll, or a hidden layout variant) is CSS, which the capture can't show. A rule of "exactly one control named Message" would refuse every profile; P4-03 must pick one by where it sits, and check that it names this profile.

Clicking **Message** opens a bubble at the bottom of the profile page; the tab stays on the profile. The click loads two requests.

## The compose requests

| Request | Method and path | Answer |
|---|---|---|
| Compose option | `GET /voyager/api/voyagerMessagingDashComposeOptions/<fsd_composeOption urn>` | `data.$type` `com.linkedin.voyager.dash.messaging.compose.ComposeOption`: `entityUrn`, `composeOptionType`, `displayText.text` (`Message`), `icon`, `textStartIcon`, `composeNavigationContext` |
| View context | `GET /voyager/api/graphql?variables=…&queryId=voyagerMessagingDashComposeViewContexts.<hash>` | `data.data.messagingDashComposeViewContextsByRecipients.elements[]`, each a `ComposeViewContext` with `showSubjectField`, `showBlockedFooter`, and header and footer keys (all `null` in the capture) |

**Where the recipient is named.** For both kinds of bubble:

- The compose option's **path**: the first part of the `fsd_composeOption` URN is the recipient's bare profile id.
- The compose option's **answer**: `data.composeNavigationContext.recipientUrns[0]` and `data.composeNavigationContext.genericRecipientsUnions[0].profile`, both `urn:li:fsd_profile:<id>`. Both matched the path's id in both captured cases.
- The view context's **request**: `variables.recipients`, `List(urn:li:fsd_profile:<id>)`. Its **answer names no recipient** and no conversation.

The two cases differ:

| | Existing conversation | Never messaged |
|---|---|---|
| `composeOptionType` | `REPLY` | `CONNECTION_MESSAGE` |
| `composeNavigationContext.existingConversationUrn` | `urn:li:fsd_conversation:<thread id>` | absent |
| View context `variables` | `recipients`, `type:REPLY`, `contextEntityUrn:<msg_conversation urn>` | `recipients`, `type:CONNECTION_MESSAGE` |
| Then | `messengerMessagesBySyncToken` for that conversation (with seen receipts and quick replies) | `messengerConversations` with `mailboxUrn` and `recipients:List(<fsd_profile urn>)`, answered `messengerConversationsByRecipients` with no elements |

`paidInMail` was `false` in both. The `existingConversationUrn` thread id, the view context's `contextEntityUrn` thread id, and the following `messengerMessages` request's thread id were the same.

## The existing-conversation bubble

From `composer.html`:

- The bubble's root is a `div` with `role="dialog"`, `aria-label="Messaging"`, `tabindex="-1"`, `data-view-name="message-overlay-conversation-bubble-item"`, and `data-msg-overlay-conversation-bubble-is-minimized="false"`.
- The `header` holds an `h2` whose only link is the recipient: `href="/in/<profile id>/"`, by **profile id, not vanity slug**, with the name as its text. Header buttons are named `Open the options list in your conversation with <name>`, `Minimize your conversation with <name>`, and `Close your conversation with <name>`.
- The messages are `li` items holding `div[data-event-urn="<msg_message urn>"][data-view-name="message-list-item"]`. A multi-line message renders as one `p` with `br` between lines. Other profile links in the bubble are absolute, `https://www.linkedin.com/in/<profile id>`.
- The composer sits in `form#msg-form-<ember id>`: one `div[contenteditable="true"][role="textbox"][aria-multiline="true"]` with `aria-label="Write a message…"` (U+2026), followed by an `aria-hidden` placeholder `div` with the same text.
- **Send** is that form's only `button[type="submit"]`, text `Send`. Beside it, a `button` with `data-test-msg-ui-send-mode-toggle-presenter__button`, text `Open send options`, holds the "Press Enter to Send" setting. Attach-image, attach-file, GIF and emoji buttons sit in the same footer.
- The captured composer held a draft, and Send was enabled. Whether Send is disabled in an empty existing-conversation composer was not captured.

## The never-messaged bubble

From `message-never-contacted.html` (the bubble's inside; whether its outer element is `role="dialog"` like the other is **not** known, because the copied HTML starts below it):

- The header's `h2` reads `New message`, with buttons `Minimize your conversation` and `Close your draft conversation`.
- A `label` `Enter message recipients` names the recipient field. The recipient is a chip: a `button[type="button"]` with `aria-label="Remove <name>"` and the name as its text, beside an `input[role="combobox"][type="text"]` with `aria-autocomplete="list"`.
- Below it, a card links to the recipient by **vanity slug**, `/in/<slug>/`, with the name and `1st degree connection`.
- The composer is the same `form#msg-form-…` with the same `role="textbox"` and `aria-label="Write a message…"`, empty (`<p><br></p>`).
- **Send** is `button[type="submit"]` with `disabled`, until there is text.

The prefill must check that there is exactly one chip, that its `Remove <name>` names the contact, and that the compose option's `recipientUrns` is the contact's URN.

## Where focus lands

Neither bubble's HTML has `autofocus`, and the `role="textbox"` element has no `tabindex`. A saved HTML file can't record `document.activeElement`, and a HAR can't either. **Where focus lands after the Message click is not known**; P4-03 must focus the composer itself, by role and name, and check it.

## Typing

While the person types in an existing conversation's composer, the page sends `POST /voyager/api/voyagerMessagingDashMessengerConversations?action=typing` with a `text/plain` JSON body of one key, `{"conversationUrn": "<msg_conversation urn>"}`, answered `202` with no body. In the captured typing, the posts came about **5 seconds apart** while typing went on (4 in one session, in two pairs; 2 before the send): the page throttles them, it doesn't post per key. The recipient's client presumably shows "typing…" while they arrive; the capture can't show the other side.

No other messaging request appeared while typing: no draft save. The category answer's `draftMessages` collection suggests LinkedIn can hold server-side drafts, but every captured one was empty.

## Sending

The person's **Send** posts `POST /voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage` (`text/plain` JSON), answered `200` `application/json`. netkeeper never sends it.

Request:

```
{"message": {"body": {"attributes": [], "text": "<text>"},
             "renderContentUnions": [],
             "conversationUrn": "<msg_conversation urn>",
             "originToken": "<36-character uuid>"},
 "mailboxUrn": "<owner's fsd_profile urn>",
 "trackingId": "<16 characters>",
 "dedupeByClientGeneratedToken": false}
```

Response:

```
{"value": {"entityUrn": "<msg_message urn>", "backendUrn": "urn:li:messagingMessage:<message id>",
           "conversationUrn": "<msg_conversation urn>", "backendConversationUrn": "urn:li:messagingThread:<thread id>",
           "senderUrn": "urn:li:msg_messagingParticipant:<owner's fsd_profile urn>",
           "originToken": "<the request's>", "body": {"attributes": [], "text": "<the request's>"},
           "deliveredAt": <ms>, "renderContentUnions": []}}
```

`value.originToken`, `value.conversationUrn` and `value.body.text` equal the request's. The owner's messages in later list and thread answers carry the same `originToken` key. No `/realtime/` event was captured for the send.

**Matching a send the person made.** The prefill doesn't send, so netkeeper never sees the `originToken` before the person clicks. The poll can match on what the list and thread answers carry: a message in the prefilled conversation (`conversation.entityUrn`, or the `createMessage` answer's `value.conversationUrn` if the tab observes it), whose `sender.hostIdentityUrn` is the mailbox owner (and whose `originToken` is therefore set), with `deliveredAt` after the prefill's hand-over. `entityUrn` identifies that message from then on. The text can confirm it, within `SNIPPET_MAX`.

## Enter and Shift+Enter

The maintainer ran the step 9 table in both the bubble and `/messaging/`, with the same results. The default setting is **Click Send** ("Press Enter to Send" off).

| "Press Enter to Send" | Key | Result |
|---|---|---|
| On | Shift+Enter | new line |
| On | Enter | sent |
| Off | Shift+Enter | new line |
| Off | Enter | new line |
| Off | ⌘+Enter | sent |

**Shift+Enter never sends, whatever the setting.** So P4-03 types a newline as Shift+Enter, flips `SHIFT_ENTER_NEWLINES_ALLOWED` in `netkeeper/linkedin/pacing.py`, and never presses Enter or ⌘+Enter; P4-11's lint allows multi-line LinkedIn templates. The capture also shows a multi-line message stored with `\n` in `body.text` and rendered with `br`.

## The bubble across pages

The maintainer's notes for step 12: a minimized bubble stays open as you move to another page, still minimized, and its draft survives. **Closing the bubble deletes the draft.** Several open bubbles can crowd the screen; P4-03 decides how many it leaves open.

## `/realtime/`

The inbox page opens `GET /realtime/connect` (`text/event-stream`), subscribes with `PUT /realtime/realtimeFrontendSubscriptions` (the captured subscription named `presenceStatusTopic`), reads `GET /realtime/realtimeFrontendTimestamp`, and posts `realtimeFrontendClientConnectivityTracking?action=sendHeartbeat`. The HAR holds no body for the event stream, so no event's shape is known. The poll reads by sync token and doesn't depend on it.

## What the capture couldn't show

- **A reply arriving live** (step 11), and any `/realtime/` event: how a new message reaches an open page, and whether the list reorders.
- **Whether a half-typed draft appears on the phone.**
- **The longest message LinkedIn accepts.** P4-11 keeps its 8,000-character assumption.
- **A live group conversation, or a system message.** Their fixtures are invented from key names. (Sponsored and InMail conversations *were* in the capture.)
- **The end of the conversation list:** whether the last `ByCategoryQuery` answer has `nextCursor: null`, an empty `elements`, or both.
- **A non-empty `ByAnchorTimestamp` answer**, and its order.
- **Where focus lands after the Message click.**
- **Which of the profile's three Message controls is visible**, and whether the never-messaged bubble's root is `role="dialog"`.
- **Whether Send is disabled in an empty existing-conversation composer.**
- **Whether the other person sees "typing…"** (the notes suggest so; the capture can't show it).

## What P4 reads, and what it assumes

**Captured** means the 2026-10-05 capture showed it; **assumed** means a reader relies on it by analogy and refuses rather than guesses when it's wrong. The first supervised poll (P4-01) and prefill (CP8) confirm every assumed row.

| What | How P4 reads it | Captured or assumed | When it doesn't read |
|---|---|---|---|
| Which answers are the list | `voyagerMessagingGraphQL/graphql` with a `queryId` named `messengerConversations`, and `data` holding `messengerConversationsBySyncToken` or `messengerConversationsByCategoryQuery` | Captured | Any other field under `data`: not the list. A list answer whose shape doesn't parse: `RouteChanged` |
| The mailbox owner | The first part of each conversation URN, the participant with `distance: SELF`, and the request's `mailboxUrn`; all three must agree | Captured | Disagreement: `RouteChanged` |
| A conversation | `entityUrn`, `lastActivityAt`, `categories`, `groupChat`, `conversationParticipants`, `messages.elements` | Captured | A missing key (other than `messages`): the whole answer is refused |
| The counterpart | The one participant whose `hostIdentityUrn` isn't the owner's, and is a `urn:li:fsd_profile:` | Captured | More than one, or a company: skipped as a group or as other |
| One-to-one | `groupChat: false`, two participants, `categories` without `INMAIL`, no `conversationTypeText`, no ad render content | Captured | |
| A group | `groupChat: true`, or more than two participants | **Assumed**: no group in the capture | Counted as `skipped_group` |
| InMail, sponsored, offers | `INMAIL` in `categories`, a `conversationTypeText`, an organization participant, `contentMetadata.conversationAdContent`, or an ad render item | Captured | Counted as `skipped_other`. An accepted InMail with a contact is skipped too; P4-01 can revisit that |
| A system message | `actor: null`; read `sender` instead | `actor: null`: captured. What a system message looks like: **assumed** | A message with no `sender`: the conversation is refused |
| The last message | `messages.elements[0]`: `entityUrn`, `sender.hostIdentityUrn`, `deliveredAt`, `body.text` | Captured | No `messages` key or no element: the conversation has no message to report |
| Outbound | `sender.hostIdentityUrn` is the owner | Captured; `originToken` agrees | |
| The text | `body.text`, cut to `SNIPPET_MAX` (200), never logged | Captured | |
| Order and completeness | Newest `lastActivityAt` first; `complete` once an item's `lastActivityAt` is at or before `since` | Order: captured | |
| The end of the list | `nextCursor` null or no elements | **Assumed** | Neither: not the end |
| Opening a thread | Navigate to `conversationUrl` (`/messaging/thread/<thread id>/`); read `messengerMessagesBySyncToken` whose request names that `conversationUrn` | Captured | No answer for that conversation: the thread unread, the list item still counts |
| A thread's messages | `elements`, newest first, the list message's shape | Captured | |
| Older messages | `messengerMessagesByAnchorTimestamp` | Shape **assumed** (both captured answers were empty) | |
| The Message control | An `<a>` named `Message` whose `href` is `/messaging/compose/?profileUrn=urn:li:fsd_profile:<id>&recipient=<id>&…`, with both ids the contact's | Captured | No control naming the contact: nothing is clicked. **Three identical controls** were captured: choose by position, never by count alone |
| The bubble opened | The compose option answer's `composeNavigationContext.recipientUrns` is `[contact's urn]` | Captured | Another recipient, or none: no key |
| Existing or new | `existingConversationUrn` present (`REPLY`) or absent (`CONNECTION_MESSAGE`) | Captured | |
| The existing bubble | One `role="dialog"` named `Messaging`, whose header `h2` link is `/in/<contact's profile id>/` | Captured | Another id, or two dialogs: no key |
| The new bubble | `New message`; exactly one chip, `Remove <name>`; the card's `/in/<slug>/` is the contact's slug | Captured (root role unknown) | Zero or two chips: no key |
| The composer | Exactly one `role="textbox"` named `Write a message…` in that bubble, empty | Captured | None, two, or not empty: no key |
| Focus | Focus the composer by role and name before typing | **Assumed**: where focus lands is unknown | |
| Newlines | Shift+Enter | Captured: never sends | |
| Send | `button[type="submit"]` named `Send`: never clicked | Captured | |
| Typing indicator | The page's own `action=typing` posts, about every 5 s | Captured | Not netkeeper's to stop |
| A send the person made | A new owner message in that conversation, `deliveredAt` after the hand-over | Captured | |
| Message length | 8,000 characters | **Assumed** (P4-11) | |

## Retire the conversations half of `voyager.py`

**Recommendation: retire it.** P4-01 deletes the conversations section of `netkeeper/linkedin/voyager.py` (`CONVERSATIONS_ENDPOINT`, `CONVERSATIONS_PATH`, `CONVERSATIONS_DECORATION_ID`, `CONVERSATIONS_DEFAULT_COUNT`, `conversations_query`, `ConversationParticipant`, `ConversationSummary`, `ConversationsPageResult`, `parse_conversations_page`, and its helpers), `tests/fixtures/voyager/conversations_page.json`, and their tests in `tests/test_linkedin_voyager.py`, and writes `netkeeper/linkedin/messaging_shapes.py` from this note. Rebuilding it doesn't pay:

- **Its path is gone.** It reads `/voyager/api/messaging/conversations` with a `decorationId`; the capture never requests that path. Under ADR 0006 netkeeper reads only what the page loads, so a parser for a path the page doesn't load can never run.
- **Its envelope is wrong.** It expects `data.elements` and `data.paging` (`start`, `count`, `total`). The GraphQL answer is `data.<field>.elements` with `metadata` holding a sync token or a cursor, and no total.
- **Its keys are wrong.** It reads participants' `firstName` and `lastName` as strings and `publicIdentifier`; the answer nests them as `participantType.member.firstName.text` and has no vanity slug, only a `profileUrl` by id. It reads `unread` (the answer has `read` and `unreadCount`) and `lastMessage` (the answer has `messages.elements`). It keeps `lastMessage.sender.entityUrn`, which in the new shape is a messaging-participant URN, not the `fsd_profile` URN matching needs.
- **It can't tell the kinds apart.** It has no `categories`, `groupChat`, or sponsored markers, so it would attribute InMail and ads.
- **Nothing calls it.** Only its tests import it. The rest of `voyager.py` (`RouteChanged`, `ConnectionSummary`, `ContactInfo`, `ProfileDetails`, and the connections parser its fake source reuses) stays. Its `_field`/`_expect` guards are worth reusing in the new parser.
