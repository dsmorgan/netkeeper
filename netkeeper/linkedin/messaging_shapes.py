"""What LinkedIn's messaging page loads, parsed whole or not at all (P4-01, #380).

Pure, behind the extractor boundary (spec 9.10, ADR 0005): response bodies and urls come
in, frozen values go out. Nothing here imports the models, opens a session, or touches a
browser. The structure follows ``docs/linkedin-messaging-shapes.md``, the maintainer's
capture of **2026-10-05** (:data:`CAPTURE_DATE`); where that note says a row is
*assumed*, the parser refuses rather than guesses when the page differs.

**Whole or not at all** (ADR 0006). Every parser raises :class:`RouteChanged` for a
body that is not the shape it expects, and never returns part of an answer: one list
item that half-parses refuses the whole list answer, so a poll never reports "no
replies" from data it could not read. The message of a ``RouteChanged`` names a shape,
never a value, so no message text or name reaches a log line.

**Which conversations are skipped, and why.** Missing a reply is the dangerous direction,
so the kind rules run in order and the first that matches decides (the shapes note's
"What P4 reads"; the fixture groups in ``tests/messaging_pages.py`` are the oracle):

1. A group (``groupChat`` true, or more than one participant besides the owner): skipped.
2. Sponsored or an offer (a company participant, ``contentMetadata``, or an ad render
   item on the last message), whatever the category, state, or label: skipped.
3. An **accepted** InMail (``INMAIL`` in ``categories``, ``state`` ``ACCEPTED``) with one
   ``fsd_profile`` counterpart: one-to-one, whatever its label, ``hostUrnData``, or
   verification label (maintainer's decision, #374 review). About a third of the captured
   inbox is like this, and a contact's only conversation may have started as InMail.
4. Any other InMail (pending, declined, or no state): skipped.
5. A ``conversationTypeText`` label or an InMail ``hostUrnData`` outside InMail: skipped.
6. Everything else is one-to-one. A ``file`` render item, a ``title``, or an ``EDITED``
   message does not disqualify a conversation.

A counterpart whose URN is neither ``fsd_profile`` nor ``fsd_company`` is an unknown
shape: :class:`RouteChanged`. A conversation with nobody but the owner is an unknown
shape too (never captured; if LinkedIn moved the counterpart, every item would look like it).
A kept conversation is matched by URN like any other; the core
ignores a stranger's.

**The mailbox owner** is read three ways that must agree: the first part of every
conversation URN, the participant whose ``member.distance`` is ``SELF``, and the
request's ``mailboxUrn`` when the url has one. **Senders are read from ``sender``,
never ``actor``**, which was null on two captured messages. A message is outbound
when its sender is the owner; a non-owner message carrying an ``originToken`` is an
unknown shape (the owner would then be misread, and a send could be confirmed
wrongly).

**Message text** is cut to :data:`~netkeeper.linkedin.inbox.SNIPPET_MAX`, is never put
in an exception, and is left out of every ``repr``.
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from netkeeper.linkedin.inbox import SNIPPET_MAX, InboxMessage
from netkeeper.linkedin.voyager import RouteChanged

#: The date of the capture these constants follow (#374).
CAPTURE_DATE: Final = "2026-10-05"

ENDPOINT: Final = "voyagerMessagingGraphQL"

#: Every list and thread answer comes from this path (captured 2026-10-05).
GRAPHQL_PATH: Final = "/voyager/api/voyagerMessagingGraphQL/graphql"
MESSAGING_PAGE_PATH: Final = "/messaging/"
THREAD_PATH_PREFIX: Final = "/messaging/thread/"

#: The part of ``queryId`` before the dot; the hash changes with LinkedIn's releases.
CONVERSATIONS_QUERY: Final = "messengerConversations"
MESSAGES_QUERY: Final = "messengerMessages"

#: The field under ``data`` that names which variant answered (captured 2026-10-05).
BY_SYNC_TOKEN: Final = "messengerConversationsBySyncToken"  # noqa: S105 -- a field name
BY_CATEGORY: Final = "messengerConversationsByCategoryQuery"
MESSAGES_BY_SYNC_TOKEN: Final = "messengerMessagesBySyncToken"  # noqa: S105 -- a field name

#: Ad items in a message's ``renderContent`` (captured 2026-10-05).
AD_RENDER_KEYS: Final = frozenset({"messageAdRenderContent", "conversationAdsMessageContent"})

PROFILE_URN_PREFIX: Final = "urn:li:fsd_profile:"
COMPANY_URN_PREFIX: Final = "urn:li:fsd_company:"

_CONVERSATION_URN: Final = re.compile(
    r"^urn:li:msg_conversation:\((urn:li:fsd_profile:[^,()\s]+),([^,()\s]+)\)$"
)
_CURSOR_FIELD: Final = re.compile(r"(?:^|[(,])nextCursor:([^,()]*)")
_BEFORE_FIELD: Final = re.compile(r"(?:^|[(,])lastUpdatedBefore:(\d+)")
_CONVERSATION_FIELD: Final = re.compile(r"conversationUrn:(urn:li:msg_conversation:\([^()]*\))")
_MAILBOX_FIELD: Final = re.compile(r"mailboxUrn:(urn:li:fsd_profile:[^,()\s]+)")


class Kind(enum.Enum):
    """What a list item is, for the poll."""

    ONE_TO_ONE = "one_to_one"
    GROUP = "group"
    OTHER = "other"  # an advertisement or a company: counted, never attributed


@dataclass(frozen=True, slots=True)
class ListItem:
    """One conversation of a list answer, fully parsed.

    ``counterpart_urn`` and ``last_message`` are set only for a :attr:`Kind.ONE_TO_ONE`
    item (the last message is ``None`` when the item carries none). ``thread_id`` is the
    id in the conversation URN, which the item's own page url must agree with.
    """

    conversation_urn: str
    thread_id: str
    owner_urn: str
    last_activity_at: datetime
    kind: Kind
    counterpart_urn: str | None = None
    last_message: InboxMessage | None = None
    #: The item's own page path, ``/messaging/thread/<id>/``, which the poll may navigate to.
    thread_path: str = ""


@dataclass(frozen=True, slots=True)
class ListAnswer:
    """A parsed list answer. ``field`` is :data:`BY_SYNC_TOKEN` or :data:`BY_CATEGORY`.

    ``next_cursor`` is the category answer's ``metadata.nextCursor`` (``None`` at the
    end of the list, assumed).
    """

    field: str
    items: tuple[ListItem, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadAnswer:
    """A parsed ``messengerMessagesBySyncToken`` answer: messages oldest first."""

    conversation_urn: str | None
    messages: tuple[InboxMessage, ...] = field(repr=False)


# --- the request -----------------------------------------------------------------------


def _query_params(url: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in urlsplit(url).query.split("&"):
        key, _, value = part.partition("=")
        if key:
            out[key] = unquote(value)
    return out


def query_name(url: str) -> str | None:
    """The ``queryId`` name (before the dot) of a messaging GraphQL request, or ``None``."""
    query_id = _query_params(url).get("queryId")
    if not query_id:
        return None
    return query_id.split(".", 1)[0] or None


def variables(url: str) -> str:
    """The Rest.li ``variables`` of the request, percent-decoded once; ``""`` if none."""
    return _query_params(url).get("variables", "")


def request_mailbox(url: str) -> str | None:
    """The request's ``mailboxUrn``, or ``None`` when it names none."""
    found = _MAILBOX_FIELD.search(variables(url))
    return found.group(1) if found else None


def request_has_sync_token(url: str) -> bool:
    """Whether this list request is a refresh (it names a ``syncToken``)."""
    return "syncToken:" in variables(url)


def request_next_cursor(url: str) -> str | None:
    found = _CURSOR_FIELD.search(variables(url))
    return unquote(found.group(1)) if found else None


_CATEGORY_FIELD: Final = re.compile(r"conversationCategoryPredicate:\(category:([A-Z_]+)\)")
#: The category the older pages of the inbox list are read from (captured 2026-10-05).
LIST_CATEGORY: Final = "PRIMARY_INBOX"


def request_category(url: str) -> str | None:
    """The category an older-page request names, or ``None``."""
    found = _CATEGORY_FIELD.search(variables(url))
    return found.group(1) if found else None


def request_last_updated_before(url: str) -> int | None:
    found = _BEFORE_FIELD.search(variables(url))
    return int(found.group(1)) if found else None


def request_conversation(url: str) -> str | None:
    """The ``conversationUrn`` a ``messengerMessages`` request names, or ``None``."""
    found = _CONVERSATION_FIELD.search(variables(url))
    return found.group(1) if found else None


def _thread_path(url: str, thread_id: str, where: str) -> str:
    """The path of a conversation's own page, which must name the urn's thread id."""
    path = urlsplit(url).path
    if not path.startswith(THREAD_PATH_PREFIX):
        raise _fail(f"{where}: 'conversationUrl' is not a thread page")
    if unquote(path[len(THREAD_PATH_PREFIX) :]).rstrip("/") != thread_id:
        raise _fail(f"{where}: 'conversationUrl' names another thread")
    return path


def conversation_parts(urn: str) -> tuple[str, str]:
    """``(owner fsd_profile urn, thread id)`` of a ``msg_conversation`` urn."""
    found = _CONVERSATION_URN.match(urn)
    if found is None:
        raise RouteChanged(ENDPOINT, "a conversation urn is not msg_conversation:(profile,thread)")
    return found.group(1), found.group(2)


# --- the guards ---------------------------------------------------------------------------


def _fail(detail: str) -> RouteChanged:
    return RouteChanged(ENDPOINT, detail)


def _obj(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _fail(f"{where}: expected an object")
    return value


def _get(obj: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise _fail(f"{where}: missing '{key}'")
    return obj[key]


def _str(obj: Mapping[str, Any], key: str, where: str) -> str:
    value = _get(obj, key, where)
    if not isinstance(value, str) or not value:
        raise _fail(f"{where}: '{key}' is not a non-empty string")
    return value


def _list(obj: Mapping[str, Any], key: str, where: str) -> list[Any]:
    value = _get(obj, key, where)
    if not isinstance(value, list):
        raise _fail(f"{where}: '{key}' is not a list")
    return value


def _millis(obj: Mapping[str, Any], key: str, where: str) -> datetime:
    value = _get(obj, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{where}: '{key}' is not an integer")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise _fail(f"{where}: '{key}' is not a valid timestamp") from None


def _load(body: bytes | str, where: str) -> Mapping[str, Any]:
    try:
        loaded = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise _fail(f"{where}: not JSON") from None
    return _obj(loaded, where)


def _data(body: bytes | str, where: str) -> Mapping[str, Any]:
    return _obj(_get(_load(body, where), "data", where), f"{where} data")


def answer_field(body: bytes | str) -> str | None:
    """The answer field under ``data`` that names the variant, or ``None`` when there is none.

    A body that is not a JSON object with an object ``data`` is :class:`RouteChanged`.
    The envelope's own bookkeeping keys (``_type``, ``_recipeType``) are not answers.
    """
    data = _data(body, "an answer")
    names = [key for key in data if not key.startswith("_")]
    known = [key for key in names if key in (BY_SYNC_TOKEN, BY_CATEGORY, MESSAGES_BY_SYNC_TOKEN)]
    if len(known) > 1:
        raise _fail("an answer names more than one variant")
    return known[0] if known else None


# --- a message ------------------------------------------------------------------------------


def _message(raw: object, *, owner: str, where: str) -> InboxMessage:
    msg = _obj(raw, where)
    urn = _str(msg, "entityUrn", where)
    sender = _obj(_get(msg, "sender", where), f"{where} sender")
    sender_urn = _str(sender, "hostIdentityUrn", f"{where} sender")
    if not sender_urn.startswith(PROFILE_URN_PREFIX):
        raise _fail(f"{where}: the sender is not a profile")
    at = _millis(msg, "deliveredAt", where)
    body = _get(msg, "body", where)
    text = ""
    if body is not None:
        text_value = _obj(body, f"{where} body").get("text")
        if text_value is not None:
            if not isinstance(text_value, str):
                raise _fail(f"{where}: body text is not a string")
            text = text_value
    outbound = sender_urn == owner
    token = _get(msg, "originToken", where)
    if token is not None and not isinstance(token, str):
        raise _fail(f"{where}: originToken is neither a string nor null")
    if token is not None and not outbound:
        # A token marks the owner's own messages. A set one on somebody else's means
        # the owner is misread, and so is which messages are sends.
        raise _fail(f"{where}: another sender's message carries an originToken")
    return InboxMessage(
        message_urn=urn,
        sender_urn=sender_urn,
        outbound=outbound,
        at=at,
        text_snippet=text[:SNIPPET_MAX],
    )


# --- the list ---------------------------------------------------------------------------------


def _text_of(value: object, where: str) -> str | None:
    if value is None:
        return None
    text = _obj(value, where).get("text")
    return text if isinstance(text, str) else None


ACCEPTED_STATE: Final = "ACCEPTED"
#: An InMail's own marker on a message's ``renderContent`` (captured 2026-10-05).
INMAIL_RENDER_KEY: Final = "hostUrnData"


def _is_inmail(item: Mapping[str, Any]) -> bool:
    categories = item.get("categories")
    return isinstance(categories, list) and "INMAIL" in categories


def _is_ad(item: Mapping[str, Any], last_raw: Mapping[str, Any] | None) -> bool:
    """Sponsored or an offer: ``contentMetadata`` or an ad item on the last message,
    whatever the category, state, or label (rule 2)."""
    content = item.get("contentMetadata")
    if isinstance(content, dict) and content.get("conversationAdContent") is not None:
        return True
    return last_raw is not None and bool(AD_RENDER_KEYS & _render_keys(last_raw))


def _is_labelled(item: Mapping[str, Any], last_raw: Mapping[str, Any] | None) -> bool:
    """A ``conversationTypeText`` label or an InMail's ``hostUrnData`` outside InMail (rule 5)."""
    if _text_of(item.get("conversationTypeText"), "conversationTypeText") is not None:
        return True
    return last_raw is not None and INMAIL_RENDER_KEY in _render_keys(last_raw)


def _render_keys(message: Mapping[str, Any]) -> set[str]:
    render = message.get("renderContent")
    keys: set[str] = set()
    if isinstance(render, list):
        for entry in render:
            if isinstance(entry, dict):
                keys.update(entry.keys())
    return keys


def _item(raw: object, index: int) -> ListItem:
    where = f"list item {index}"
    item = _obj(raw, where)
    urn = _str(item, "entityUrn", where)
    owner, thread_id = conversation_parts(urn)
    last_activity = _millis(item, "lastActivityAt", where)
    _list(item, "categories", where)
    group_chat = _get(item, "groupChat", where)
    if not isinstance(group_chat, bool):
        raise _fail(f"{where}: 'groupChat' is not a boolean")
    participants = [
        _obj(p, f"{where} participant") for p in _list(item, "conversationParticipants", where)
    ]
    selves = []
    others = []
    for participant in participants:
        host = _str(participant, "hostIdentityUrn", f"{where} participant")
        member = _obj(_get(participant, "participantType", where), f"{where} type").get("member")
        if isinstance(member, dict) and member.get("distance") == "SELF":
            selves.append(host)
        elif host == owner:
            # The owner again, not marked SELF: never the counterpart.
            raise _fail(f"{where}: the owner takes part twice")
        else:
            others.append(host)
    if selves != [owner]:
        raise _fail(f"{where}: the SELF participant is not the owner in the conversation urn")

    last_raw: Mapping[str, Any] | None = None
    messages = item.get("messages")
    if messages is not None:
        elements = _list(_obj(messages, f"{where} messages"), "elements", f"{where} messages")
        if len(elements) > 1:
            raise _fail(f"{where}: the list carries more than one message")
        if elements:
            last_raw = _obj(elements[0], f"{where} message")

    if group_chat or len(others) > 1:
        return ListItem(urn, thread_id, owner, last_activity, Kind.GROUP)
    if not others:
        # An owner-only conversation was never captured. If LinkedIn moved the
        # counterpart, every item would look like this: refuse rather than skip them all.
        raise _fail(f"{where}: nobody but the owner takes part")
    counterpart = others[0]
    # The kind rules, in order; the first that matches decides (the shapes note):
    # 1 a group (above), 2 sponsored or an offer, 3 an accepted InMail with one profile
    # counterpart, 4 any other InMail, 5 a label or hostUrnData outside InMail, 6 the rest.
    if counterpart.startswith(COMPANY_URN_PREFIX) or _is_ad(item, last_raw):
        return ListItem(urn, thread_id, owner, last_activity, Kind.OTHER)
    if not counterpart.startswith(PROFILE_URN_PREFIX):
        raise _fail(f"{where}: the counterpart is neither a profile nor a company")
    accepted = item.get("state") == ACCEPTED_STATE
    if _is_inmail(item):
        if not accepted:
            return ListItem(urn, thread_id, owner, last_activity, Kind.OTHER)
    elif _is_labelled(item, last_raw):
        return ListItem(urn, thread_id, owner, last_activity, Kind.OTHER)
    message = (
        None if last_raw is None else _message(last_raw, owner=owner, where=f"{where} message")
    )
    path = _thread_path(_str(item, "conversationUrl", where), thread_id, where)
    return ListItem(
        urn, thread_id, owner, last_activity, Kind.ONE_TO_ONE, counterpart, message, path
    )


def parse_conversation_list(body: bytes | str) -> ListAnswer:
    """Parse a ``messengerConversationsBySyncToken`` or ``...ByCategoryQuery`` answer.

    Every item must parse, or the whole answer is :class:`RouteChanged`. The owner must
    be the same in every item.
    """
    name = answer_field(body)
    if name not in (BY_SYNC_TOKEN, BY_CATEGORY):
        raise _fail("not a conversation list answer")
    collection = _obj(_get(_data(body, "list"), name, "list"), "the list")
    elements = _list(collection, "elements", "the list")
    items = tuple(_item(raw, i) for i, raw in enumerate(elements))
    if len({i.owner_urn for i in items}) > 1:
        raise _fail("the list names more than one mailbox owner")
    cursor: str | None = None
    if name == BY_CATEGORY:
        metadata = _obj(_get(collection, "metadata", "the list"), "the list metadata")
        value = _get(metadata, "nextCursor", "the list metadata")
        if value is not None and not isinstance(value, str):
            raise _fail("the list metadata: 'nextCursor' is neither a string nor null")
        cursor = value or None
    return ListAnswer(name, items, cursor)


# --- a thread ---------------------------------------------------------------------------------


def parse_thread(
    body: bytes | str, *, owner_urn: str, conversation_urn: str | None
) -> ThreadAnswer:
    """Parse a ``messengerMessagesBySyncToken`` answer; messages come back oldest first.

    Every message must parse, and name ``conversation_urn`` (when given) as its
    conversation, or the whole answer is :class:`RouteChanged`.
    """
    if answer_field(body) != MESSAGES_BY_SYNC_TOKEN:
        raise _fail("not a thread answer")
    collection = _obj(_get(_data(body, "thread"), MESSAGES_BY_SYNC_TOKEN, "thread"), "the thread")
    elements = _list(collection, "elements", "the thread")
    messages: list[InboxMessage] = []
    for index, raw in enumerate(elements):
        where = f"thread message {index}"
        message = _message(raw, owner=owner_urn, where=where)
        if conversation_urn is not None:
            conversation = _obj(_get(raw, "conversation", where), f"{where} conversation")
            if _str(conversation, "entityUrn", where) != conversation_urn:
                raise _fail(f"{where}: it belongs to another conversation")
        messages.append(message)
    messages.sort(key=lambda m: m.at)
    return ThreadAnswer(conversation_urn, tuple(messages))
