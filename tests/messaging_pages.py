"""LinkedIn messaging answers and HTML for tests: the captured shape, with invented people.

Nothing here came from a capture. The people, ids, tokens, timestamps and message text
are invented (``ACoAAInvented`` profile ids, ``-fake-`` slugs, companies that do not
exist), and every payload is assembled by the functions below from the *structure*
recorded in ``docs/linkedin-messaging-shapes.md`` (the maintainer's capture of
2026-10-05, #374): the GraphQL envelope, the key names, the ``_type`` names, the enum
values, how the URNs nest, and the roles and accessible names of the controls. Values
the parsers never read (pictures, tracking, LinkedIn's recipe hashes) are trimmed or
replaced with an obvious placeholder. ``tests/test_messaging_pages.py`` checks that no
string here appears in the capture when the capture is on this machine.

Where the capture showed no live example (a group conversation, a system message),
the shape is marked as **invented**: it follows the key names the capture did show,
and a parser must treat it as unconfirmed.

What is here, and who builds on it:

* The inbox poll, P4-01 (#380): :func:`conversations_by_sync_token`,
  :func:`conversations_by_category`, :func:`conversations_by_ids`,
  :func:`messages_by_sync_token`, :func:`messages_by_anchor`, the request urls the
  page sends for them (:func:`conversations_sync_url` and the rest), and the kinds of
  conversation in :data:`INBOX_FIRST_PAGE` and :data:`INBOX_OLDER_PAGE`.
* The prefill, P4-03 (#382): :func:`profile_message_controls_html`,
  :func:`existing_bubble_html`, :func:`never_messaged_bubble_html`, the compose answers
  (:func:`compose_option_answer`, :func:`compose_view_contexts_answer`), the typing
  request (:func:`typing_body`), and :func:`create_message_request` /
  :func:`create_message_response` for matching a send the person made.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Any, Final
from urllib.parse import quote, urlencode

#: The date of the capture these shapes follow (local time; 2026-10-06 UTC).
CAPTURE_DATE: Final = "2026-10-05"

# --- requests, as captured ------------------------------------------------------------

HOST: Final = "https://www.linkedin.com"
#: Every list and thread answer comes from this path, ``GET``, ``application/graphql``.
GRAPHQL_PATH: Final = "/voyager/api/voyagerMessagingGraphQL/graphql"
#: The query names, the part of ``queryId`` before the dot. The hash after the dot
#: differs per variant and changes with LinkedIn's releases: match the name only.
CONVERSATIONS_QUERY: Final = "messengerConversations"
MESSAGES_QUERY: Final = "messengerMessages"
#: The field under ``data`` that names which variant answered.
BY_SYNC_TOKEN: Final = "messengerConversationsBySyncToken"
BY_CATEGORY: Final = "messengerConversationsByCategoryQuery"
BY_IDS: Final = "messengerConversationsByIds"
BY_RECIPIENTS: Final = "messengerConversationsByRecipients"
MESSAGES_BY_SYNC_TOKEN: Final = "messengerMessagesBySyncToken"
MESSAGES_BY_ANCHOR: Final = "messengerMessagesByAnchorTimestamp"
MESSAGES_BY_SYNC_TOKENS_IN_BATCH: Final = "messengerMessagesBySyncTokensInBatch"
#: The general Voyager GraphQL path, which the compose view context query uses.
VOYAGER_GRAPHQL_PATH: Final = "/voyager/api/graphql"
COMPOSE_VIEW_CONTEXTS_QUERY: Final = "voyagerMessagingDashComposeViewContexts"
COMPOSE_VIEW_CONTEXTS_FIELD: Final = "messagingDashComposeViewContextsByRecipients"
COMPOSE_OPTIONS_PATH: Final = "/voyager/api/voyagerMessagingDashComposeOptions/"
#: ``POST``, body ``{"conversationUrn": ...}``, answered 202 with no body.
TYPING_PATH: Final = "/voyager/api/voyagerMessagingDashMessengerConversations?action=typing"
#: ``POST``; the page's own send. netkeeper never sends it (ADR 0006, ADR 0007).
CREATE_MESSAGE_PATH: Final = (
    "/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage"
)
#: The page size the list asks for.
CONVERSATIONS_PAGE_SIZE: Final = 20
#: What scrolling up a thread asks for, before and after the anchor.
ANCHOR_COUNT_BEFORE: Final = 20
ANCHOR_COUNT_AFTER: Final = 0

#: The categories seen. A conversation carries two or three of them.
INBOX: Final = "INBOX"
PRIMARY_INBOX: Final = "PRIMARY_INBOX"
SECONDARY_INBOX: Final = "SECONDARY_INBOX"
INMAIL: Final = "INMAIL"
ARCHIVE: Final = "ARCHIVE"

#: The labels ``conversationTypeText.text`` showed (LinkedIn's own words, not data).
SPONSORED_LABEL: Final = "Sponsored"
INMAIL_LABEL: Final = "InMail"
OFFER_LABEL: Final = "LinkedIn Offer"

# --- the controls, as captured --------------------------------------------------------

#: The Message control's visible text, which is its accessible name.
MESSAGE_CONTROL_TEXT: Final = "Message"
#: The composer's accessible name. The last character is U+2026, not three dots.
COMPOSER_LABEL: Final = "Write a message…"
#: The existing-conversation bubble's ``role="dialog"`` name.
BUBBLE_DIALOG_LABEL: Final = "Messaging"
#: The never-messaged bubble's heading.
NEW_MESSAGE_HEADING: Final = "New message"
#: The send-mode toggle beside Send ("Press Enter to Send" lives behind it).
SEND_OPTIONS_LABEL: Final = "Open send options"
#: The compose link's fixed parameters on a profile.
COMPOSE_SCREEN_CONTEXT: Final = "NON_SELF_PROFILE_VIEW"
COMPOSE_INTEROP: Final = "msgOverlay"

_ID_LENGTH: Final = 39  # the captured profile ids' length
_TYPE_PREFIX: Final = "com.linkedin."
_RECIPE: Final = "com.linkedin.inventedRecipe"  # LinkedIn's recipe hashes, replaced
_COLLECTION: Final = "com.linkedin.restli.common.CollectionResponse"
_TEXT: Final = "com.linkedin.pemberly.text.AttributedText"
_PARTICIPANT: Final = "com.linkedin.messenger.MessagingParticipant"


# --- the invented cast ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Member:
    """One invented LinkedIn member."""

    n: int
    first: str
    last: str
    headline: str
    distance: str = "DISTANCE_1"

    @property
    def profile_id(self) -> str:
        """The id after ``urn:li:fsd_profile:``: invented, padded to the captured length."""
        return f"ACoAAInvented{self.n:07d}".ljust(_ID_LENGTH, "x")

    @property
    def urn(self) -> str:
        return f"urn:li:fsd_profile:{self.profile_id}"

    @property
    def slug(self) -> str:
        return f"{self.first.lower()}-fake-{self.last.lower()}-{self.n:04d}"

    @property
    def member_urn(self) -> str:
        """``backendUrn``: the numeric member id, invented."""
        return f"urn:li:member:{900_000_000 + self.n}"

    @property
    def participant_urn(self) -> str:
        return f"urn:li:msg_messagingParticipant:{self.urn}"

    @property
    def profile_url(self) -> str:
        """``member.profileUrl``: by profile id, not by vanity slug, as captured."""
        return f"{HOST}/in/{self.profile_id}"

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"


@dataclass(frozen=True, slots=True)
class Organization:
    """An invented company, as a sponsored conversation's participant."""

    n: int
    name: str

    @property
    def urn(self) -> str:
        return f"urn:li:fsd_company:{8_000_000 + self.n}"

    @property
    def participant_urn(self) -> str:
        return f"urn:li:msg_messagingParticipant:{self.urn}"


#: The mailbox owner: the account netkeeper runs as.
OWNER: Final = Member(1, "Odalys", "Inventova", "Owner of this invented mailbox", "SELF")
ZEPHYRINE: Final = Member(101, "Zephyrine", "Mockwell", "Data engineer at Fictional Robotics Co")
THADDEUS: Final = Member(102, "Thaddeus", "Placeholdt", "Head of design at Acme Testing Group")
MARISOL: Final = Member(
    103, "Marisol", "Notreal", "Recruiter at Placeholder Partners", "DISTANCE_2"
)
BRIXTON: Final = Member(104, "Brixton", "Fauxley", "Founder, Imaginary Analytics")
QUILLON: Final = Member(105, "Quillon", "Shamwick", "SRE at Nonexistent Networks")
SAFFRON: Final = Member(106, "Saffron", "Pretendo", "PM at Example Widgets Ltd", "DISTANCE_3")
SPONSOR: Final = Organization(1, "Hypothetical Holdings Ads")


# --- ids -------------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("ascii")).decode("ascii")


def thread_id(n: int) -> str:
    """``2-`` and base64 of ``<uuid>_<3 digits>``, as captured; the uuid is invented."""
    return "2-" + _b64(f"00000000-0000-4000-8000-{n:012d}_100")


def conversation_urn(n: int, owner: Member = OWNER) -> str:
    """``urn:li:msg_conversation:(<the mailbox's fsd_profile urn>,<thread id>)``."""
    return f"urn:li:msg_conversation:({owner.urn},{thread_id(n)})"


def fsd_conversation_urn(n: int) -> str:
    """The compose option's ``existingConversationUrn``: the same thread id, no mailbox."""
    return f"urn:li:fsd_conversation:{thread_id(n)}"


def thread_url(n: int) -> str:
    """``conversationUrl``: the thread's own page, which the poll navigates to."""
    return f"{HOST}/messaging/thread/{thread_id(n)}/"


def message_id(conversation: int, k: int, at_ms: int) -> str:
    """``2-`` and base64 of ``<ms>b<5 digits>-<3 digits>&<the thread's uuid part>``."""
    tail = base64.b64decode(thread_id(conversation)[2:]).decode("ascii")
    return "2-" + _b64(f"{at_ms}b{k:05d}-100&{tail}")


def message_urn(conversation: int, k: int, at_ms: int, owner: Member = OWNER) -> str:
    return f"urn:li:msg_message:({owner.urn},{message_id(conversation, k, at_ms)})"


def origin_token(n: int) -> str:
    """A client-made uuid: set on the mailbox owner's own messages, ``None`` on others'."""
    return f"00000000-1111-4222-8333-{n:012d}"


#: Invented epoch milliseconds, 13 digits as captured.
T0: Final = 1_790_000_000_000
MINUTE_MS: Final = 60_000


# --- the JSON parts ---------------------------------------------------------------------


def _text(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"_type": _TEXT, "_recipeType": _RECIPE, "attributes": [], "text": value}


def participant(who: Member | Organization, *, brief: bool = False) -> dict[str, Any]:
    """A ``MessagingParticipant``. ``brief`` is a message's ``sender``: no ``participantType``."""
    if isinstance(who, Organization):
        host = who.urn
        kind: dict[str, Any] = {
            "member": None,
            "organization": {
                "_type": "com.linkedin.messenger.OrganizationParticipantInfo",
                "_recipeType": _RECIPE,
                "name": _text(who.name),
                "tagline": None,
                "industryName": None,
                "pageType": None,
                "pageUrl": None,
                "following": False,
                "averageResponseTimeText": None,
                "logo": None,
            },
            "agent": None,
            "custom": None,
        }
        backend = f"urn:li:company:{8_000_000 + who.n}"
    else:
        host = who.urn
        kind = {
            "member": {
                "_type": "com.linkedin.messenger.MemberParticipantInfo",
                "_recipeType": _RECIPE,
                "profileUrl": who.profile_url,
                "firstName": _text(who.first),
                "lastName": _text(who.last),
                "headline": _text(who.headline),
                "distance": who.distance,
                "pronoun": None,
                "profilePicture": None,
                "profileFrameA11yContent": None,
            },
            "organization": None,
            "agent": None,
            "custom": None,
        }
        backend = who.member_urn
    base: dict[str, Any] = {
        "_type": _PARTICIPANT,
        "_recipeType": _RECIPE,
        "entityUrn": who.participant_urn,
        "hostIdentityUrn": host,
        "memberBadgeType": None,
        "showPremiumInBug": False,
        "showVerificationBadge": False,
    }
    if brief:
        return base
    return {**base, "participantType": kind, "backendUrn": backend, "preview": None}


@dataclass(frozen=True, slots=True)
class Msg:
    """One invented message, before it becomes JSON."""

    conversation: int
    k: int
    sender: Member | Organization
    text: str
    at_ms: int
    #: ``None`` makes the message actorless, as two captured list messages were.
    actor: Member | Organization | None = None
    subject: str | None = None
    render_content: tuple[dict[str, Any], ...] = ()
    edited: bool = False

    @property
    def urn(self) -> str:
        return message_urn(self.conversation, self.k, self.at_ms)

    @property
    def outbound(self) -> bool:
        return self.sender == OWNER


def message(m: Msg, *, actorless: bool = False) -> dict[str, Any]:
    """A ``com.linkedin.messenger.Message``, as both the list and a thread carry it."""
    mid = message_id(m.conversation, m.k, m.at_ms)
    return {
        "_type": "com.linkedin.messenger.Message",
        "_recipeType": _RECIPE,
        "entityUrn": m.urn,
        "backendUrn": f"urn:li:messagingMessage:{mid}",
        "backendConversationUrn": f"urn:li:messagingThread:{thread_id(m.conversation)}",
        "conversation": {
            "_type": "com.linkedin.messenger.Conversation",
            "_recipeType": _RECIPE,
            "entityUrn": conversation_urn(m.conversation),
        },
        "body": _text(m.text),
        "subject": m.subject,
        "deliveredAt": m.at_ms,
        "actor": None if actorless else participant(m.actor or m.sender),
        "sender": participant(m.sender, brief=True),
        "originToken": origin_token(m.conversation * 1000 + m.k) if m.outbound else None,
        "messageBodyRenderFormat": "EDITED" if m.edited else "DEFAULT",
        "renderContent": list(m.render_content),
        "renderContentFallbackText": None,
        "reactionSummaries": [],
        "footer": None,
        "inlineWarning": None,
        "incompleteRetriableData": False,
    }


_DISABLED_ONE_TO_ONE: Final = (
    "UPDATE_MESSAGE_REQUEST_STATE",
    "ADD_PARTICIPANT",
    "RENAMED_CONVERSATION",
    "REMOVE_PARTICIPANT",
    "CREATE_GROUP_CHAT_LINK",
)


@dataclass(frozen=True, slots=True)
class Conv:
    """One invented conversation, before it becomes JSON."""

    n: int
    others: tuple[Member | Organization, ...]
    last: Msg | None
    categories: tuple[str, ...] = (INBOX, PRIMARY_INBOX)
    group_chat: bool = False
    #: The message-request state: ``PENDING``, ``ACCEPTED``, ``DECLINED``, or ``None``.
    state: str | None = None
    type_label: str | None = None
    title: str | None = None
    ad_content: bool = False
    creator: Member | Organization = OWNER
    #: ``False`` leaves out the ``messages`` key, as two captured list items did.
    has_messages: bool = True
    #: The category answer carries an (empty) ``draftMessages`` collection; the sync one does not.
    drafts_key: bool = False

    @property
    def urn(self) -> str:
        return conversation_urn(self.n)


def conversation(c: Conv) -> dict[str, Any]:
    """A ``com.linkedin.messenger.Conversation`` as the list carries it: the last message only."""
    last_ms = c.last.at_ms if c.last else T0
    out: dict[str, Any] = {
        "_type": "com.linkedin.messenger.Conversation",
        "_recipeType": _RECIPE,
        "entityUrn": c.urn,
        "backendUrn": f"urn:li:messagingThread:{thread_id(c.n)}",
        "conversationUrl": thread_url(c.n),
        "categories": list(c.categories),
        "groupChat": c.group_chat,
        "state": c.state,
        "title": c.title,
        "conversationTypeText": _text(c.type_label),
        "conversationVerificationLabel": None,
        "conversationVerificationExplanation": None,
        "headlineText": None,
        "shortHeadlineText": None,
        "descriptionText": None,
        "contentMetadata": _ad_content_metadata() if c.ad_content else None,
        "conversationParticipants": [participant(OWNER), *(participant(o) for o in c.others)],
        "creator": participant(c.creator),
        "createdAt": last_ms - 30 * MINUTE_MS,
        "lastActivityAt": last_ms,
        "lastReadAt": last_ms,
        "read": True,
        "unreadCount": 0,
        "notificationStatus": "ACTIVE",
        "disabledFeatures": [
            {
                "_type": "com.linkedin.messenger.ConversationDisabledFeature",
                "_recipeType": _RECIPE,
                "disabledFeature": feature,
                "reasonText": None,
            }
            for feature in _DISABLED_ONE_TO_ONE
        ],
        "hostConversationActions": [],
        "incompleteRetriableData": False,
    }
    if c.has_messages:
        out["messages"] = {
            "_type": _COLLECTION,
            "_recipeType": _RECIPE,
            "elements": [message(c.last)] if c.last else [],
        }
    if c.drafts_key:
        out["draftMessages"] = {"_type": _COLLECTION, "_recipeType": _RECIPE, "elements": []}
    return out


def _ad_content_metadata() -> dict[str, Any]:
    """A sponsored conversation's ``contentMetadata``, trimmed to its type names."""
    return {
        "conversationAdContent": {
            "_type": "com.linkedin.messenger.ConversationAdsContent",
            "_recipeType": _RECIPE,
            "advertiserLegalText": None,
            "creativeAdsReportingInfo": None,
            "sponsoredTracking": None,
            "adUnit": None,
        }
    }


def message_ad_render_content() -> dict[str, Any]:
    """A sponsored message's ``renderContent`` item (``messageAdRenderContent``), trimmed."""
    return {
        "messageAdRenderContent": {
            "_type": "com.linkedin.messenger.MessageAdRenderContent",
            "_recipeType": _RECIPE,
            "status": "PENDING",
            "advertiserLabel": None,
            "legalText": None,
            "sponsoredCampaignUrn": f"urn:li:sponsoredCampaign:{700_000_001}",
            "subContent": None,
            "creativeAdsReportingInfo": None,
            "sponsoredTracking": None,
            "bodyTracking": None,
            "openTracking": None,
            "legalTextTracking": None,
        }
    }


def conversation_ads_render_content() -> dict[str, Any]:
    """A sponsored conversation's ``renderContent`` item (``conversationAdsMessageContent``)."""
    return {
        "conversationAdsMessageContent": {
            "_type": "com.linkedin.messenger.ConversationAdsMessageContent",
            "_recipeType": _RECIPE,
            "sponsoredMessageContentUrn": (
                "urn:li:sponsoredMessageContent:(urn:li:sponsoredConversation:600000001,600000002)"
            ),
            "sponsoredMessageOptions": [],
            "sponsoredMessageTrackingId": "invented-tracking-id",
        }
    }


def host_urn_render_content(kind: str, sender: Member) -> dict[str, Any]:
    """An InMail's ``hostUrnData`` item; ``kind`` was ``SALES_INMAIL`` or ``PREMIUM_INMAIL``."""
    return {
        "hostUrnData": {
            "_type": "com.linkedin.messenger.HostUrnData",
            "_recipeType": _RECIPE,
            "type": kind,
            "hostUrn": sender.urn,
        }
    }


def _envelope(field: str, collection: dict[str, Any] | list[Any]) -> str:
    """The GraphQL envelope: ``{"data": {"_type", "_recipeType", <field>: ...}}``, no
    ``included``."""
    return json.dumps({"data": {"_type": _RECIPE, "_recipeType": _RECIPE, field: collection}})


def _collection(elements: list[dict[str, Any]], metadata: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"_type": _COLLECTION, "_recipeType": _RECIPE, "elements": elements}
    if metadata is not None:
        out["metadata"] = metadata
    return out


# --- the list ---------------------------------------------------------------------------


def conversations_by_sync_token(
    convs: Sequence[Conv],
    *,
    new_sync_token: str = "invented-sync-token-conversations-0001",
    deleted_urns: Sequence[str] | None = None,
) -> str:
    """The list on load (``mailboxUrn`` only), or a refresh (``syncToken`` too).

    The first load's metadata has ``newSyncToken`` alone; a refresh's adds
    ``deletedUrns`` and ``shouldClearCache``. Newest ``lastActivityAt`` first.
    """
    metadata: dict[str, Any] = {
        "_type": "com.linkedin.messenger.SyncMetadata",
        "_recipeType": _RECIPE,
        "newSyncToken": new_sync_token,
    }
    if deleted_urns is not None:
        metadata["deletedUrns"] = list(deleted_urns)
        metadata["shouldClearCache"] = False
    return _envelope(BY_SYNC_TOKEN, _collection([conversation(c) for c in convs], metadata))


def conversations_by_category(convs: Sequence[Conv], *, next_cursor: str | None) -> str:
    """Older conversations, as the list scrolls: ``ConversationCursorMetadata.nextCursor``.

    **Invented:** the end of the list. The capture never reached it; ``next_cursor=None``
    is a guess at how it ends.
    """
    metadata = {
        "_type": "com.linkedin.messenger.ConversationCursorMetadata",
        "_recipeType": _RECIPE,
        "nextCursor": next_cursor,
    }
    with_drafts = [_with_drafts(c) for c in convs]
    return _envelope(BY_CATEGORY, _collection([conversation(c) for c in with_drafts], metadata))


def _with_drafts(c: Conv) -> Conv:
    return Conv(**{**{f: getattr(c, f) for f in Conv.__dataclass_fields__}, "drafts_key": True})


def conversations_by_ids(convs: Sequence[Conv]) -> str:
    """``conversationIds`` lookup: the field holds a bare **list**, not a collection."""
    return _envelope(BY_IDS, [conversation(c) for c in convs])


def conversations_by_recipients(convs: Sequence[Conv] = ()) -> str:
    """The never-messaged bubble's lookup by ``recipients``: no ``metadata``; empty when new."""
    return _envelope(BY_RECIPIENTS, _collection([conversation(c) for c in convs], None))


# --- a thread ---------------------------------------------------------------------------


def messages_by_sync_token(
    msgs: Sequence[Msg], *, new_sync_token: str = "invented-sync-token-messages-0001"
) -> str:
    """A thread on open: newest ``deliveredAt`` first, as captured."""
    ordered = sorted(msgs, key=lambda m: m.at_ms, reverse=True)
    metadata = {
        "_type": "com.linkedin.messenger.MessageMetadata",
        "_recipeType": _RECIPE,
        "newSyncToken": new_sync_token,
        "deletedUrns": [],
        "shouldClearCache": False,
    }
    return _envelope(MESSAGES_BY_SYNC_TOKEN, _collection([message(m) for m in ordered], metadata))


def messages_by_anchor(
    msgs: Sequence[Msg], *, prev_cursor: str | None = None, next_cursor: str | None = None
) -> str:
    """Older messages, as the thread scrolls up. The capture's two answers were empty, so
    the order of a non-empty one is **assumed** to be the same as the sync answer's."""
    ordered = sorted(msgs, key=lambda m: m.at_ms, reverse=True)
    metadata = {
        "_type": "com.linkedin.messenger.MessageMetadata",
        "_recipeType": _RECIPE,
        "prevCursor": prev_cursor,
        "nextCursor": next_cursor,
    }
    return _envelope(MESSAGES_BY_ANCHOR, _collection([message(m) for m in ordered], metadata))


# --- the request urls -------------------------------------------------------------------


def _q(urn: str) -> str:
    return quote(urn, safe="")


def _graphql_url(query: str, variables: str, *, path: str = GRAPHQL_PATH) -> str:
    return f"{HOST}{path}?queryId={query}.0123456789abcdefinventedhash&variables={variables}"


def conversations_sync_url(sync_token: str | None = None, owner: Member = OWNER) -> str:
    tail = f",syncToken:{sync_token}" if sync_token else ""
    return _graphql_url(CONVERSATIONS_QUERY, f"(mailboxUrn:{_q(owner.urn)}{tail})")


def conversations_category_url(
    *,
    category: str = PRIMARY_INBOX,
    last_updated_before: int | None = None,
    next_cursor: str | None = None,
    owner: Member = OWNER,
) -> str:
    """The first older page names ``lastUpdatedBefore``; later ones ``nextCursor``."""
    anchor = (
        f"lastUpdatedBefore:{last_updated_before}"
        if next_cursor is None
        else f"nextCursor:{quote(next_cursor, safe='')}"
    )
    query = f"(predicateUnions:List((conversationCategoryPredicate:(category:{category}))))"
    return _graphql_url(
        CONVERSATIONS_QUERY,
        f"(query:{query},count:{CONVERSATIONS_PAGE_SIZE},mailboxUrn:{_q(owner.urn)},{anchor})",
    )


def conversations_ids_url(*conversations: int) -> str:
    ids = ",".join(_q(conversation_urn(n)) for n in conversations)
    return _graphql_url(
        CONVERSATIONS_QUERY, f"(conversationIds:List({ids}),count:{len(conversations)})"
    )


def conversations_recipients_url(recipient: Member, owner: Member = OWNER) -> str:
    return _graphql_url(
        CONVERSATIONS_QUERY, f"(mailboxUrn:{_q(owner.urn)},recipients:List({_q(recipient.urn)}))"
    )


def messages_sync_url(conversation: int, sync_token: str | None = None) -> str:
    tail = f",syncToken:{sync_token}" if sync_token else ""
    return _graphql_url(
        MESSAGES_QUERY, f"(conversationUrn:{_q(conversation_urn(conversation))}{tail})"
    )


def messages_anchor_url(conversation: int, delivered_at: int) -> str:
    return _graphql_url(
        MESSAGES_QUERY,
        f"(deliveredAt:{delivered_at},conversationUrn:{_q(conversation_urn(conversation))},"
        f"countBefore:{ANCHOR_COUNT_BEFORE},countAfter:{ANCHOR_COUNT_AFTER})",
    )


# --- composing --------------------------------------------------------------------------


def compose_option_urn(recipient: Member) -> str:
    """The path's urn: the recipient's **bare profile id** first, then the screen context.

    The third part is a 24-character token, invented here.
    """
    parts = (recipient.profile_id, COMPOSE_SCREEN_CONTEXT, "inventedcomposetoken0001")
    return f"urn:li:fsd_composeOption:({','.join(parts)})"


def compose_option_url(recipient: Member) -> str:
    return f"{HOST}{COMPOSE_OPTIONS_PATH}{_q(compose_option_urn(recipient))}"


def compose_option_answer(recipient: Member, *, existing_conversation: int | None) -> str:
    """``voyagerMessagingDashComposeOptions/<urn>``: normalized JSON, ``included`` empty.

    The recipient is ``data.composeNavigationContext.recipientUrns[0]`` and
    ``...genericRecipientsUnions[0].profile``. ``existingConversationUrn`` is there only
    when a conversation exists; ``composeOptionType`` is then ``REPLY``, otherwise
    ``CONNECTION_MESSAGE``.
    """
    context: dict[str, Any] = {
        "$type": "com.linkedin.voyager.dash.messaging.compose.ComposeNavigationContext",
        "genericRecipientsUnions": [{"profile": recipient.urn}],
        "recipientUrns": [recipient.urn],
        "paidInMail": False,
    }
    if existing_conversation is not None:
        context["existingConversationUrn"] = fsd_conversation_urn(existing_conversation)
    icon = {
        "$type": "com.linkedin.voyager.dash.common.image.ImageViewModel",
        "attributes": [
            {
                "$type": "com.linkedin.voyager.dash.common.image.ImageAttribute",
                "detailDataUnion": {"icon": "SYS_ICN_INVENTED"},
            }
        ],
    }
    return json.dumps(
        {
            "data": {
                "$type": "com.linkedin.voyager.dash.messaging.compose.ComposeOption",
                "entityUrn": compose_option_urn(recipient),
                "composeOptionType": "REPLY"
                if existing_conversation is not None
                else "CONNECTION_MESSAGE",
                "displayText": {
                    "$type": "com.linkedin.voyager.dash.common.text.TextViewModel",
                    "text": MESSAGE_CONTROL_TEXT,
                    "textDirection": "USER_LOCALE",
                    "attributesV2": [],
                },
                "composeNavigationContext": context,
                "icon": icon,
                "textStartIcon": icon,
            },
            "included": [],
        }
    )


def compose_view_contexts_url(recipient: Member, *, existing_conversation: int | None) -> str:
    """The recipient is only in the **request**: ``variables.recipients``."""
    if existing_conversation is None:
        variables = f"(recipients:List({_q(recipient.urn)}),type:CONNECTION_MESSAGE)"
    else:
        variables = (
            f"(recipients:List({_q(recipient.urn)}),type:REPLY,"
            f"contextEntityUrn:{_q(conversation_urn(existing_conversation))})"
        )
    return _graphql_url(COMPOSE_VIEW_CONTEXTS_QUERY, variables, path=VOYAGER_GRAPHQL_PATH)


def compose_view_contexts_answer() -> str:
    """The view context answer. It names **no** recipient and no conversation."""
    element = {
        "$type": "com.linkedin.voyager.dash.messaging.compose.ComposeViewContext",
        "$recipeTypes": [_RECIPE],
        "showSubjectField": False,
        "showBlockedFooter": False,
        "contextText": None,
        "footerText": None,
        "headerText": None,
        "footer": None,
        "footerIcon": None,
        "trustInterventionPage": None,
        "header": None,
        "invitationText": None,
        "headerIcon": None,
        "headerTitle": None,
    }
    return json.dumps(
        {
            "data": {
                "data": {
                    "$type": _RECIPE,
                    "$recipeTypes": [_RECIPE],
                    COMPOSE_VIEW_CONTEXTS_FIELD: {
                        "$type": _COLLECTION,
                        "$recipeTypes": [_RECIPE],
                        "elements": [element],
                    },
                }
            },
            "included": [],
        }
    )


def typing_body(conversation: int) -> str:
    """The typing request's body (``text/plain`` JSON). It names the conversation only."""
    return json.dumps({"conversationUrn": conversation_urn(conversation)})


def create_message_request(
    conversation: int, text: str, *, token: str, tracking_id: str = "inventedtrack001"
) -> str:
    """What the page posts when the **person** clicks Send. netkeeper never posts it."""
    return json.dumps(
        {
            "message": {
                "body": {"attributes": [], "text": text},
                "renderContentUnions": [],
                "conversationUrn": conversation_urn(conversation),
                "originToken": token,
            },
            "mailboxUrn": OWNER.urn,
            "trackingId": tracking_id,
            "dedupeByClientGeneratedToken": False,
        }
    )


def create_message_response(conversation: int, k: int, text: str, *, token: str, at_ms: int) -> str:
    """The send's answer (``application/json``): ``value`` echoes the conversation, the
    body and the ``originToken``, and names the new message and its sender."""
    mid = message_id(conversation, k, at_ms)
    return json.dumps(
        {
            "value": {
                "renderContentUnions": [],
                "entityUrn": message_urn(conversation, k, at_ms),
                "backendConversationUrn": f"urn:li:messagingThread:{thread_id(conversation)}",
                "senderUrn": OWNER.participant_urn,
                "originToken": token,
                "body": {"attributes": [], "text": text},
                "backendUrn": f"urn:li:messagingMessage:{mid}",
                "conversationUrn": conversation_urn(conversation),
                "deliveredAt": at_ms,
            }
        }
    )


# --- the canned conversations -----------------------------------------------------------

#: One-to-one, the last message from the contact.
INBOUND_LAST: Final = Msg(
    11, 3, ZEPHYRINE, "Invented reply about the fictional robotics meetup.", T0 + 50 * MINUTE_MS
)
ONE_TO_ONE_INBOUND: Final = Conv(11, (ZEPHYRINE,), INBOUND_LAST, creator=ZEPHYRINE)
#: One-to-one, the last message the owner's own (``originToken`` set).
ONE_TO_ONE_OUTBOUND: Final = Conv(
    12,
    (THADDEUS,),
    Msg(12, 2, OWNER, "Invented line one\nInvented line two", T0 + 40 * MINUTE_MS),
)
#: An InMail the owner accepted: categories include ``INMAIL``, ``state`` ``ACCEPTED``.
INMAIL_ACCEPTED: Final = Conv(
    13,
    (MARISOL,),
    Msg(
        13,
        1,
        MARISOL,
        "Invented recruiter note about an imaginary role.",
        T0 + 30 * MINUTE_MS,
        subject="Invented subject line",
    ),
    categories=(INBOX, PRIMARY_INBOX, INMAIL),
    state="ACCEPTED",
    creator=MARISOL,
)
#: A pending InMail with ``hostUrnData`` and the ``InMail`` label.
INMAIL_PENDING: Final = Conv(
    14,
    (BRIXTON,),
    Msg(
        14,
        1,
        BRIXTON,
        "Invented pitch from a founder who does not exist.",
        T0 + 20 * MINUTE_MS,
        subject="Invented pitch subject",
        render_content=(host_urn_render_content("SALES_INMAIL", BRIXTON),),
    ),
    categories=(INBOX, PRIMARY_INBOX, INMAIL),
    state="PENDING",
    type_label=INMAIL_LABEL,
    creator=BRIXTON,
)
#: A sponsored message from a company: ``ARCHIVE``/``INMAIL``, ``Sponsored``, ad content.
SPONSORED: Final = Conv(
    15,
    (SPONSOR,),
    Msg(
        15,
        1,
        SPONSOR,
        "Invented advertisement text.",
        T0 + 10 * MINUTE_MS,
        render_content=(conversation_ads_render_content(),),
    ),
    categories=(ARCHIVE, INMAIL),
    type_label=SPONSORED_LABEL,
    ad_content=True,
    creator=SPONSOR,
)
#: **Invented:** a group. The capture had none; ``groupChat`` true, three or more participants.
GROUP: Final = Conv(
    16,
    (QUILLON, SAFFRON),
    Msg(16, 4, QUILLON, "Invented group message about a pretend offsite.", T0 + 5 * MINUTE_MS),
    group_chat=True,
    title="Invented group title",
    creator=QUILLON,
)
#: A list item with no ``messages`` key, as two captured items had.
NO_MESSAGES: Final = Conv(17, (THADDEUS,), None, has_messages=False)

INBOX_FIRST_PAGE: Final = (ONE_TO_ONE_INBOUND, ONE_TO_ONE_OUTBOUND, INMAIL_ACCEPTED, INMAIL_PENDING)
INBOX_OLDER_PAGE: Final = (SPONSORED, GROUP, NO_MESSAGES)

#: **Invented as a whole:** a system message. The capture's nearest thing was a list
#: message with ``actor: null`` (and a ``sender``) in an ordinary conversation.
ACTORLESS: Final = Msg(18, 1, ZEPHYRINE, "Invented notice text.", T0 + 2 * MINUTE_MS)

#: The thread of :data:`ONE_TO_ONE_INBOUND`, three messages.
THREAD_ONE_TO_ONE: Final = (
    Msg(11, 1, OWNER, "Invented opener about a pretend conference.", T0 + 10 * MINUTE_MS),
    Msg(
        11,
        2,
        ZEPHYRINE,
        "Invented answer, line one\nInvented answer, line two",
        T0 + 30 * MINUTE_MS,
    ),
    INBOUND_LAST,
)


def actorless_list_item() -> dict[str, Any]:
    """A list item whose last message has ``actor: null``, as captured twice."""
    item = conversation(Conv(18, (ZEPHYRINE,), ACTORLESS, creator=ZEPHYRINE))
    item["messages"]["elements"] = [message(ACTORLESS, actorless=True)]
    return item


# --- HTML -------------------------------------------------------------------------------


def compose_href(recipient: Member, *, absolute: bool = False) -> str:
    """The Message control's ``href``. The server-rendered profile writes it relative; the
    control copied from the live page was absolute. ``recipient`` is the bare id."""
    query = urlencode(
        {
            "profileUrn": recipient.urn,
            "recipient": recipient.profile_id,
            "screenContext": COMPOSE_SCREEN_CONTEXT,
            "interop": COMPOSE_INTEROP,
        }
    )
    return f"{HOST if absolute else ''}/messaging/compose/?{query}"


def message_control_html(recipient: Member, *, absolute: bool = True) -> str:
    """One Message control, as captured: an ``<a>`` whose text is the name, with its
    sibling "More" menu button. No ``aria-label``; ``role`` is the ``<a>``'s own."""
    return (
        "<div><div data-display-contents>"
        f'<a aria-disabled="false" href="{escape(compose_href(recipient, absolute=absolute))}">'
        '<span><svg aria-hidden="true"></svg><span>Message</span></span></a></div>'
        '<div data-display-contents><button type="button" aria-expanded="false">'
        "<span><span>More</span></span></button></div></div>"
    )


def profile_message_controls_html(
    recipient: Member, *, copies: int = 3, decoy: Member | None = None
) -> str:
    """A profile page with ``copies`` Message controls for one recipient, as the captured
    document had three. Which of them a person sees is not known from the capture.
    ``decoy`` adds a "People also viewed" card with another person's control
    (**invented**, for the prefill's refusal tests)."""
    controls = "".join(
        f'<div componentkey="invented-message-control-{i}">'
        f"{message_control_html(recipient, absolute=False)}</div>"
        for i in range(copies)
    )
    extra = (
        f"<aside><h2>People also viewed</h2>{message_control_html(decoy, absolute=False)}</aside>"
        if decoy is not None
        else ""
    )
    return f"<main><h1>{escape(recipient.name)}</h1>{controls}</main>{extra}"


def _composer(draft: str) -> str:
    para = f"<p>{escape(draft)}</p>" if draft else "<p><br></p>"
    return (
        f'<div contenteditable="true" role="textbox" dir="auto" aria-multiline="true" '
        f'aria-label="{COMPOSER_LABEL}">{para}</div>'
        f'<div aria-hidden="true" data-placeholder="{COMPOSER_LABEL}"></div>'
    )


def _send_footer(*, disabled: bool) -> str:
    return (
        "<footer>"
        f'<button {"disabled " if disabled else ""}type="submit">Send</button>'
        '<button type="button" aria-expanded="false" '
        "data-test-msg-ui-send-mode-toggle-presenter__button>"
        f"<span>{SEND_OPTIONS_LABEL}</span></button>"
        "</footer>"
    )


def existing_bubble_html(recipient: Member, msgs: Sequence[Msg] = (), *, draft: str = "") -> str:
    """The bubble for an existing conversation, as ``composer.html`` showed it.

    ``role="dialog"`` named "Messaging"; the header's ``h2`` holds a link to
    ``/in/<profile id>/`` (the id, not the slug) with the name; each message is a
    ``data-event-urn`` item; the composer and Send sit in ``form#msg-form-<n>``. Send
    is enabled whether or not the composer is empty (captured with text in it; empty
    is **assumed**).
    """
    items = "".join(
        '<li><div data-event-urn="{urn}" data-view-name="message-list-item">'
        '<div dir="ltr"><p>{text}</p></div></div></li>'.format(
            urn=escape(m.urn), text="<br>".join(escape(x) for x in m.text.split("\n"))
        )
        for m in msgs
    )
    name = escape(recipient.name)
    return (
        '<div id="ember-invented-1" data-msg-overlay-conversation-bubble-open="" '
        'data-msg-overlay-conversation-bubble-is-minimized="false" tabindex="-1" role="dialog" '
        f'aria-label="{BUBBLE_DIALOG_LABEL}" '
        'data-view-name="message-overlay-conversation-bubble-item">'
        '<header tabindex="0">'
        f'<h2 tabindex="-1"><a href="/in/{recipient.profile_id}/">{name}</a></h2>'
        '<button type="button" aria-expanded="false">'
        f"<span>Open the options list in your conversation with {name}</span></button>"
        f'<button aria-expanded="true"><span>Minimize your conversation with {name}</span></button>'
        f"<button><span>Close your conversation with {name}</span></button>"
        "</header>"
        f"<ul>{items}</ul>"
        '<form id="msg-form-ember-invented-2">'
        f"{_composer(draft)}{_send_footer(disabled=False)}</form>"
        "</div>"
    )


def never_messaged_bubble_html(chips: Sequence[Member], *, draft: str = "") -> str:
    """The bubble for someone never messaged, as ``message-never-contacted.html`` showed it.

    A "New message" heading; each recipient a chip, a button named ``Remove <name>``,
    beside a ``role="combobox"`` search field labelled "Enter message recipients"; a
    profile link ``/in/<slug>/`` for the recipient; Send ``disabled`` until there is text.
    The captured copy was the bubble's inside: whether its outer element is
    ``role="dialog"`` is **not** known.
    """
    chip_html = "".join(
        f'<button aria-label="Remove {escape(m.name)}" type="button">'
        f"<span>{escape(m.name)}</span></button>"
        for m in chips
    )
    cards = "".join(f'<a href="/in/{m.slug}/">{escape(m.name)}</a>' for m in chips)
    return (
        "<div>"
        f'<header tabindex="0"><h2 tabindex="-1">{NEW_MESSAGE_HEADING}</h2>'
        '<button aria-expanded="true"><span>Minimize your conversation</span></button>'
        "<button><span>Close your draft conversation</span></button></header>"
        '<label for="ember-invented-3-search-field">Enter message recipients</label>'
        f'<section><div tabindex="-1">{chip_html}'
        '<input id="ember-invented-3-search-field" role="combobox" type="text" autocomplete="off" '
        'aria-autocomplete="list" aria-expanded="false"></div></section>'
        f"<div>{cards}</div>"
        '<form id="msg-form-ember-invented-4">'
        f"{_composer(draft)}{_send_footer(disabled=not draft)}</form>"
        "</div>"
    )


# --- every value here, for the no-copy check -------------------------------------------

#: Strings that are structure, not data: they appear in the capture by design.
STRUCTURAL: Final = frozenset(
    {
        MESSAGE_CONTROL_TEXT,
        COMPOSER_LABEL,
        BUBBLE_DIALOG_LABEL,
        NEW_MESSAGE_HEADING,
        SEND_OPTIONS_LABEL,
        SPONSORED_LABEL,
        INMAIL_LABEL,
        OFFER_LABEL,
        "More",
        "Send",
        "Minimize your conversation",
        "Close your draft conversation",
        "Enter message recipients",
        "People also viewed",
        "true",
        "false",
        "auto",
        "ltr",
        "text",
        "list",
        "off",
        "submit",
        "button",
        "combobox",
        "textbox",
        "dialog",
        "-1",
        "0",
        "",
        "message-list-item",
        "message-overlay-conversation-bubble-item",
    }
)


def is_structural(value: str) -> bool:
    """A key name, a type name, an enum, or a fixed label the capture shows by design."""
    return (
        value in STRUCTURAL
        or (value.startswith(_TYPE_PREFIX) and value != _RECIPE)
        or value.isupper()
        or value.replace("_", "").isupper()
    )
