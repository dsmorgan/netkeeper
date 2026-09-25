# LinkedIn flagship-web shapes

This note records the structure of what LinkedIn's `flagship-web` client loads for the connections list, a profile, and the contact-info overlay, as the maintainer's capture of 2026-09-24 showed it (#149). It records **structure only**: row kinds, keys, `$type` names, identifiers the page uses for its own components, and counts. It holds no name, slug, profile id, email address, phone number, headline, or url of any person. The capture stays in the maintainer's private folder; nothing in this repository was copied from it.

[ADR 0006](adr/0006-observe-dont-request.md) records why netkeeper reads these answers rather than requesting anything itself. The connections parser is `netkeeper/linkedin/flagship.py`; the hand-built fixtures that follow these shapes, with invented people, are `tests/flagship_pages.py`. The profile and contact-info fixtures there exist for the enrichment lane, which has no parser yet.

## The flight grammar

Every answer below is a React Server Components *flight* payload, `application/octet-stream`, one row per line:

| Row | Example shape | Meaning |
|---|---|---|
| `<hex id>:I[...]` | `1:I["<chunk>",[],"TriggerButton"]` | A client component the page imports. Other rows refer to it by id. |
| `<hex id>:<JSON>` | `0:[...]`, `4:{...}`, `7:null`, `15:"$Sreact.suspense"` | A model row. |
| `<hex id>:T<hex length>,<text>` | | A text row, length-prefixed in bytes. The text can contain newlines. |
| `<hex id>:E{...}` | | The server's error for that row. |
| `:HL[...]` and other upper-case tags | | Hints: fonts, preloads. |

A rendered element is a four-item list, `["$", <type>, <key>, <props>]`. `<type>` is an HTML tag (`"div"`, `"p"`) or a reference to a component row (`"$L3"`). A string `$<id>` or `$L<id>` in a model refers to row `<id>`; the page factors each card of a list into rows of its own this way. `$undefined` and `$S...` are React's own markers, not references.

Component names seen: `ClientComponent`, `VisibleItemsProvider`, `SduiTrackingScopeWrapper`, `TriggerButton`, `DesignSystemImage`, and several named `default`.

Actions are objects with a `$type`. The ones the parsers rely on:

- `proto.sdui.actions.core.SetState`: `{value: {state: {key: {key: {value: {$case: "id", id: <state id>}}, namespace}, value: {$case: <kind>, <kind>: <value>}}}}`. Kinds seen: `stringValue`, `booleanValue`, `intValue`, `imageAssetValue`, `expression`.
- `proto.sdui.actions.core.Navigate`: `{value: {content: {$case: "screen", screen: {$type: "proto.sdui.actions.core.NavigateToScreen", screenId, url, presentation, requestedArguments: {payload: {...}}}}}}` for a screen in the app, or `{content: {$case: "url", url: {$type: "proto.sdui.actions.core.NavigateToUrl", urlValue: {$case: "url", url}}}}` for a link out.
- Triggers wrap actions: `{$type: "proto.sdui.triggers.Trigger", type: {$case: "click", ...}, action: {actions: [...]}}`.

## The connections list

### Loading the page

A full page load of `GET /mynetwork/invite-connect/connections/` answers with an HTML document. Its first screen is inside `<script id="rehydrate-data">`, which assigns `window.__como_rehydration__` an array of strings. Joined, the strings are one flight payload. (The capture shows this for `/mynetwork`; the connections page itself was reached in-app, so this document form is inferred from the same client. `PageConnections` reads either form.)

An in-app navigation to the same screen sends `POST /flagship-web/mynetwork/invite-connect/connections`, whose body is a `NavigateToScreen` object, and receives the same payload directly.

The first screen carries:

- **10 cards**, keyed `ConnectionCard_0-<slug>`.
- **The total**: a `modelStates` entry `{key: {key: {value: {$case: "id", id: "totalConnectionsCount"}}}, value: {$case: "intValue", intValue: <n>}}`, on the element at the root of row `0`. Two text components also bind `totalConnectionsCount` with `defaultValue: 0`; those are display bindings, not the count.
- **The next request**: see below.

### Scrolling

When the list nears the bottom of the viewport, the page sends `POST /flagship-web/rsc-action/actions/pagination?parentSpanId=...&sduiid=...` with a JSON body:

```
{
  "pagerId": "com.linkedin.sdui.pagers.mynetwork.connectionsList",
  "clientArguments": {
    "$type": "proto.sdui.actions.requests.RequestedArguments",
    "requestedStateKeys": [{"key": {"value": {"$case": "id", "id": "connectionsListSortOption"}}, "namespace": "connectionsListSortOptionMenu"}],
    "payload": {"startIndex": <n>, "sortByOptionBinding": {"key": "connectionsListSortOption", "namespace": "connectionsListSortOptionMenu"}},
    "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
    "states": [{"key": "connectionsListSortOption", "namespace": "connectionsListSortOptionMenu", "value": "sortByRecentlyAdded", "originalProtoCase": "stringValue", "protoKey": {...}}],
    "screenId": "com.linkedin.sdui.flagshipnav.mynetwork.Connections",
    "knownTemplateIds": []
  },
  "paginationRequest": { ... the same request, as the previous answer carried it ... }
}
```

`sortByRecentlyAdded` is the default, and the capture's requests all carry it. The `/mynetwork` page's "people you may know" cohorts post to the same path with `pagerId` `com.linkedin.sdui.pagers.mynetwork.scribeCohortBackfill`, a different `payload` (`pageSize` 6, a `pageToken`), and answers with no cards: those answers are not the connections list and are not its end.

The capture has 19 connections pagination requests, `startIndex` 10 through 190 in steps of 10, each answered in 0.25 to 0.55 s, several seconds apart as the scroll allowed.

### An answer

Row `0` is a list of nine items. The ones that matter:

| Index | Shape | Meaning |
|---|---|---|
| `[0]` | `[<provider element with modelStates>, "$undefined"]` | On the first screen, `modelStates` holds the total. |
| `[1]` | a JSON **string** | The next request: `{$type: "proto.sdui.actions.requests.PaginationRequest", pagerId: "...connectionsList", trigger: {$case: "itemDistanceTrigger", itemDistanceTrigger: {preloadDistance: 3, preloadLength: 250}}, retryCount: 2, requestedArguments: {payload: {startIndex: <start + 10>, sortByOptionBinding: ...}}}`. |
| `[2]` | a list of 20 `[<item key>, <element>]` pairs | Alternating: a divider `div`, then a card. |

Each card is an element (`$L<ClientComponent>`) whose props hold `componentKey: "ConnectionCard_<startIndex>-<slug>"`. `<startIndex>` is the start of the page that answer is for (0 for the first screen). Inside it, a `div` with the same key as `componentkey` (lower case), then:

- **An image link row** (`$L<n>`): `SduiTrackingScopeWrapper` → `ClientComponent` (`componentKey: "ConnectionCardProfileImage_<start>-<slug>"`) → `TriggerButton` with one click trigger whose actions are, in order:
  1. `SetState` `profile_name_loading_state` = the display name, one string ("First Last", sometimes with more words).
  2. `SetState` `profile_headline_loading_state` = the headline.
  3. `SetState` of the photo (`imageAssetValue`, image urls).
  4. `SetState` of a boolean.
  5. `Navigate` → `NavigateToScreen`, `screenId: "com.linkedin.sdui.flagshipnav.profile.Profile"`, `url: "/in/<slug>/"`, `requestedArguments.payload: {vanityName: <slug>, isVanityNameResolved: true, vieweeProfileId: <id>}`.
- **A name link row**: the same trigger, under a component with its own key.
- **A text element** whose `textProps.children` is `["Connected on <Month d, yyyy>"]`.
- **A menu row**: the card's overflow menu. Its "Remove connection" confirmation carries `RemoveUi` (keyed by the card's `ConnectionCard_...` key), `Focus`, `AddToStringList`, a `SetState` of `totalConnectionsCount` as an expression (total minus one), and a `ServerRequest` `com.linkedin.sdui.mynetwork.RemoveConnectionVanityName` with `disconnectVanityName`. netkeeper never clicks it.

`vieweeProfileId` is the id behind `urn:li:fsd_profile:<id>`: 39 characters of `[A-Za-z0-9_-]`, starting `ACo`. Every card in the capture had exactly one identity (the two links agree), a `vanityName` equal to its key's slug, one name, one headline, and one "Connected on" date. 200 cards, 200 distinct ids.

### The end of the list

The capture never reached it. What an answer past the last connection looks like is **not known**; the maintainer's decision is that an answer with no cards ends the list. `flagship.py` accepts two signals as the end: a connections answer with no cards, or a short answer (fewer than ten) whose slot `[1]` carries no next request. A full answer without a next request proves nothing, and a page that stops asking proves nothing. The first supervised full sync should confirm which signal LinkedIn sends; the fixtures invent `"$undefined"` for the empty slot.

## A profile

An in-app navigation to a profile sends `POST /flagship-web/in/<slug>/` (0.9 to 1.0 s) and receives the profile screen as flight. A full page load of `/in/<slug>/` is a document, as above. The first HAR (`capture-2026-09-24.har`) instead shows the older Voyager calls for a profile, `identity/dash/profiles/<urn>` and the `voyagerIdentityDashProfiles` GraphQL query, so LinkedIn serves some sessions one client and some the other.

- **Seeds.** The screen carries the same `SetState` seeds a card's click sets: `profile_name_loading_state`, `profile_headline_loading_state`, `profile_photo_loading_state`, `profile_loading_has_data`.
- **Names.** `firstName` and `lastName` appear as separate strings in action payloads on the page (the message and follow buttons' `requestedArguments`, sometimes with `vanityName` or `profileUrn` beside them), and as `givenName` and `familyName` in the Contact info link's payload.
- **The top card**, an element with `viewTrackingSpecs.viewName: "profile-top-card"`. Its text runs include, in this order: the degree (`· 1st`), the headline, the location (`City, Region, Country` in one run), a `·`, the **Contact info** link, and the connection count (`500+ connections`), then shared connections. Short runs that look like the current company and school sit among them; which is which is unverified.
- **The Contact info link** is an element in the top card's `textProps.children` with `children: ["Contact info"]`, `linkStyle`, and an `action` of one `Navigate` → `NavigateToScreen`: `screenId: "com.linkedin.sdui.flagshipnav.profile.ProfileContactDetailsOverlay"`, `url: "/in/<slug>/overlay/contact-info/"`, `presentation: {$case: "modal"}`, `requestedArguments.payload: {vanityName, givenName, familyName, isVanityNameResolved: true}`. Its `viewTrackingSpecs` is `$undefined`. This is the one control ADR 0006 allows a click on.
- **Experience** is an element with `viewName: "profile-card-experience"`, headed by a text element `{tagName: "h2", children: ["Experience"]}`. Each role renders as text runs, not fields: the title; `<Company> · <Employment type>` (`Full-time`, `Part-time`, ...); `<Mon YYYY> - <Mon YYYY | Present> · <n yrs m mos>`; the location; then description bullets. Several roles at one company render as a company header with `<Employment type> · <total duration>` and the roles under it. A role's company logo is a link with `viewName: "experience-company-logo-click"` and the company page's url. Other `viewName`s in the card: `experience-see-media-button`, `experience-media-roll-up`, and on the member's own profile `experience-edit-button`, `experience-add-position-button`, `experience-add-career-break-button`.
- **Lazy cards.** The rest of the profile loads as the page scrolls, by `POST /flagship-web/rsc-action/actions/component?componentId=com.linkedin.sdui.generated.profile.dsl.impl.<name>`, with names `profileCardsAboveActivity` (About, highlights), `profileCardsActivity`, `profileCardsExperienceOnly`, `profileCardsBelowActivityPart1WithoutExp`, and `profileCardsBelowActivityPart2` through `Part7`. Experience was inline on three of the four captured profiles; on the fourth it was lazy (`profileCardsExperienceOnly`) and was not captured. **Education was not seen in any captured answer**; it is presumably in one of the `BelowActivity` parts. Its shape is unverified.

## The contact-info overlay

Clicking **Contact info** sends `POST /flagship-web/rsc-action/actions/navigation?screenId=com.linkedin.sdui.flagshipnav.profile.ProfileContactDetailsOverlay&sduiid=...` (0.8 s) with a JSON body:

```
{
  "clientArguments": {
    "$type": "proto.sdui.actions.requests.RequestedArguments",
    "requestedStateKeys": [],
    "payload": {"vanityName": <slug>, "givenName": <first>, "familyName": <last>, "isVanityNameResolved": true},
    "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
    "states": [],
    "screenId": "com.linkedin.sdui.flagshipnav.profile.ProfileContactDetailsOverlay",
    "knownTemplateIds": []
  },
  "isModal": true
}
```

The answer is small (about 36 KB, ten model rows). One row holds a `div` with a `data-testid` and a paginated list of sections. Each section is an element whose `viewTrackingSpecs` names it:

| `viewName` | `legacyControlName` | Heading | Values |
|---|---|---|---|
| `contact-your-profile` | `contact_share_profile` | (the profile link heading) | one link to the member's own profile url |
| `contact-website` | `contact_website` | `Website` | one link per site; the link's url goes through a `linkedin.com` redirect wrapper, its text is the site, and a short text follows it |
| `contact-email` | `contact_email` | `Email` | one link whose url is `mailto:<address>` and whose text is the address |

After the sections, a `p` with `Connected since` and the date as text.

Each value is a link element (`$L<n>`) whose `action` is one `Navigate` → `NavigateToUrl` (`urlValue.url`), with `openInNewTab`, `urlType`, and `presentation` fields, and whose `children` are the text shown. The captured profile shared a website and an email and no phone, so **the phone, address, birthday, and messaging sections are unverified**; by analogy they are probably `contact-phone` and so on, and the fixtures use `contact-phone` for that reason, marked invented.

## Elsewhere on these pages

- Messaging still uses Voyager GraphQL (`voyagerMessagingGraphQL/graphql?queryId=messengerConversations...`): a phase 4 concern.
- Bot detection is present: PerimeterX sensors (`protechts`, `px-cloud` collectors), reCAPTCHA Enterprise, and obfuscated telemetry posts on every page.
- Some actions stream: `GET /flagship-web/rsc-action/actions/server-stream-request` (`text/event-stream`). Nothing netkeeper reads uses them.
