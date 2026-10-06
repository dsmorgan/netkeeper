"""The messaging parser (P4-01, #380): whole or not at all, and never a quiet "no replies".

Built from :mod:`messaging_pages`' invented fixtures. Each attack below is a test: can a
half-parsed answer hand on part of a list? Is a reply's conversation skipped as an ad or an
InMail? Is an unknown shape read as an empty inbox?
"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import messaging_pages as mp
import pytest

from netkeeper.linkedin import messaging_shapes as shapes
from netkeeper.linkedin.inbox import SNIPPET_MAX
from netkeeper.linkedin.messaging_shapes import Kind
from netkeeper.linkedin.voyager import RouteChanged


def _first() -> shapes.ListAnswer:
    return shapes.parse_conversation_list(mp.conversations_by_sync_token(mp.INBOX_FIRST_PAGE))


def _doc(body: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(body)
    return loaded


def _field_items(doc: dict[str, Any], field: str = mp.BY_SYNC_TOKEN) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = doc["data"][field]["elements"]
    return items


# --- the happy path -------------------------------------------------------------------


def test_the_first_page_parses_in_order_with_the_owner_and_counterparts() -> None:
    answer = _first()

    assert answer.field == mp.BY_SYNC_TOKEN
    assert [i.conversation_urn for i in answer.items] == [
        mp.conversation_urn(n) for n in (11, 12, 13, 14)
    ]
    assert {i.owner_urn for i in answer.items} == {mp.OWNER.urn}
    inbound, outbound = answer.items[0], answer.items[1]
    assert (inbound.kind, inbound.counterpart_urn) == (Kind.ONE_TO_ONE, mp.ZEPHYRINE.urn)
    assert inbound.last_message is not None and not inbound.last_message.outbound
    assert inbound.last_message.sender_urn == mp.ZEPHYRINE.urn
    assert inbound.last_message.at == datetime.fromtimestamp(mp.INBOUND_LAST.at_ms / 1000, tz=UTC)
    assert outbound.last_message is not None and outbound.last_message.outbound
    assert outbound.last_message.sender_urn == mp.OWNER.urn
    assert outbound.last_message.text_snippet == "Invented line one\nInvented line two"
    assert inbound.thread_path == f"/messaging/thread/{mp.thread_id(11)}/"


def _kind_of(c: mp.Conv) -> Kind:
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([c])).items
    return item.kind


@pytest.mark.parametrize("c", mp.READ_AS_ONE_TO_ONE, ids=lambda c: f"conv{c.n}")
def test_the_fixtures_read_as_one_to_one_are_one_to_one(c: mp.Conv) -> None:
    """An accepted InMail is kept beside ordinary conversations; a file, a title, and an
    edited message do not disqualify one."""
    assert _kind_of(c) is Kind.ONE_TO_ONE


@pytest.mark.parametrize("c", mp.SKIPPED_OTHER, ids=lambda c: f"conv{c.n}")
def test_the_fixtures_skipped_as_other_are(c: mp.Conv) -> None:
    assert _kind_of(c) is Kind.OTHER


@pytest.mark.parametrize("c", mp.SKIPPED_GROUP, ids=lambda c: f"conv{c.n}")
def test_the_fixtures_skipped_as_group_are(c: mp.Conv) -> None:
    assert _kind_of(c) is Kind.GROUP


def test_the_accepted_inmail_keeps_its_counterpart_and_last_message() -> None:
    accepted = _first().items[2]
    assert mp.INMAIL in mp.INMAIL_ACCEPTED.categories
    assert accepted.counterpart_urn == mp.MARISOL.urn and accepted.last_message is not None


@pytest.mark.parametrize("state", ["PENDING", "DECLINED", None])
def test_an_inmail_the_person_has_not_accepted_is_skipped(state: str | None) -> None:
    assert _kind_of(_with(mp.INMAIL_ACCEPTED, state=state)) is Kind.OTHER


def test_an_ad_a_company_and_a_group_are_skipped_by_kind() -> None:
    older = shapes.parse_conversation_list(
        mp.conversations_by_category(
            (mp.SPONSORED, mp.GROUP, mp.NO_MESSAGES), next_cursor="invented-cursor-1"
        )
    )

    kinds = [i.kind for i in older.items]
    assert kinds == [Kind.OTHER, Kind.GROUP, Kind.ONE_TO_ONE]
    assert older.next_cursor == "invented-cursor-1"
    assert older.items[2].last_message is None  # no `messages` key: nothing to report
    assert older.items[0].counterpart_urn is None and older.items[1].last_message is None


def _with(conv: mp.Conv, **changes: Any) -> mp.Conv:
    return mp.Conv(**{**{f: getattr(conv, f) for f in mp.Conv.__dataclass_fields__}, **changes})


@pytest.mark.parametrize(
    "label",
    [mp.SPONSORED_LABEL, mp.OFFER_LABEL],
)
def test_linkedins_own_ad_labels_skip(label: str) -> None:
    conv = _with(mp.ONE_TO_ONE_INBOUND, type_label=label)
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([conv])).items
    assert item.kind is Kind.OTHER


def test_ad_content_metadata_skips_even_without_a_label() -> None:
    conv = _with(mp.ONE_TO_ONE_INBOUND, ad_content=True)
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([conv])).items
    assert item.kind is Kind.OTHER


@pytest.mark.parametrize(
    "render", [mp.message_ad_render_content(), mp.conversation_ads_render_content()]
)
def test_an_ad_render_item_on_the_last_message_skips(render: dict[str, Any]) -> None:
    last = mp.Msg(11, 3, mp.ZEPHYRINE, "Invented.", mp.T0, render_content=(render,))
    conv = _with(mp.ONE_TO_ONE_INBOUND, last=last)
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([conv])).items
    assert item.kind is Kind.OTHER


def test_other_contentmetadata_is_not_an_ad_mark() -> None:
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))
    [item] = _field_items(doc)
    item["contentMetadata"] = {"somethingElse": {"_type": "com.linkedin.invented"}}
    [parsed] = shapes.parse_conversation_list(json.dumps(doc)).items
    assert parsed.kind is Kind.ONE_TO_ONE


def test_a_sponsored_label_alone_does_not_skip_an_accepted_inmail() -> None:
    assert _kind_of(_with(mp.INMAIL_ACCEPTED, type_label=mp.SPONSORED_LABEL)) is Kind.ONE_TO_ONE


def test_no_counterpart_is_an_unknown_shape_not_a_skipped_item() -> None:
    """An owner-only conversation was never captured; if LinkedIn moved the counterpart,
    every item would look like it, and skipping them all would read as "no replies"."""
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))
    [item] = _field_items(doc)
    item["conversationParticipants"] = item["conversationParticipants"][:1]
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))


def test_an_owner_participant_not_marked_self_is_refused() -> None:
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))
    [item] = _field_items(doc)
    owner = item["conversationParticipants"][0]
    assert owner["participantType"]["member"]["distance"] == "SELF"
    owner["participantType"]["member"]["distance"] = "DISTANCE_1"
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))


def test_self_marked_on_another_urn_than_the_conversation_owner_is_refused() -> None:
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))
    [item] = _field_items(doc)
    item["conversationParticipants"][1]["participantType"]["member"]["distance"] = "SELF"
    item["conversationParticipants"][0]["participantType"]["member"]["distance"] = "DISTANCE_1"
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))


def test_the_owner_listed_again_as_a_counterpart_is_refused() -> None:
    """One participant marked SELF, plus the owner's URN again not marked SELF: it must not
    parse as one-to-one with the owner as the counterpart."""
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))
    [item] = _field_items(doc)
    twin = deepcopy(item["conversationParticipants"][0])
    twin["participantType"]["member"]["distance"] = "DISTANCE_1"
    item["conversationParticipants"] = [item["conversationParticipants"][0], twin]
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))


def test_two_participants_marked_self_are_refused() -> None:
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))
    [item] = _field_items(doc)
    item["conversationParticipants"][1]["participantType"]["member"]["distance"] = "SELF"
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))


def test_the_older_page_category_is_read_from_the_request() -> None:
    assert shapes.request_category(mp.conversations_category_url(last_updated_before=1)) == (
        "PRIMARY_INBOX"
    )
    assert shapes.request_category(mp.conversations_sync_url()) is None


def test_hostUrnData_outside_inmail_is_skipped() -> None:
    item = mp.host_urn_render_content("SALES_INMAIL", mp.ZEPHYRINE)
    last = mp.Msg(11, 3, mp.ZEPHYRINE, "Invented.", mp.T0, render_content=(item,))
    assert _kind_of(_with(mp.ONE_TO_ONE_INBOUND, last=last)) is Kind.OTHER


def test_a_file_render_item_does_not_disqualify_a_conversation() -> None:
    file_item = {"file": {"_type": "com.linkedin.messenger.File", "name": "invented.pdf"}}
    last = mp.Msg(11, 3, mp.ZEPHYRINE, "Invented.", mp.T0, render_content=(file_item,))
    assert _kind_of(_with(mp.ONE_TO_ONE_INBOUND, last=last)) is Kind.ONE_TO_ONE


def test_the_group_flag_alone_makes_a_group() -> None:
    conv = _with(mp.ONE_TO_ONE_INBOUND, group_chat=True)
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([conv])).items
    assert item.kind is Kind.GROUP


def test_a_company_counterpart_is_skipped_even_with_no_ad_mark() -> None:
    last = mp.Msg(11, 3, mp.SPONSOR, "Invented.", mp.T0)
    conv = mp.Conv(11, (mp.SPONSOR,), last)
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([conv])).items
    assert item.kind is Kind.OTHER and item.counterpart_urn is None


def test_the_owners_own_message_is_outbound_even_without_an_origin_token() -> None:
    """A send from another client may carry no token; the sender decides, not the token."""
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_OUTBOUND]))
    [item] = _field_items(doc)
    assert item["messages"]["elements"][0]["originToken"] is not None
    item["messages"]["elements"][0]["originToken"] = None
    [parsed] = shapes.parse_conversation_list(json.dumps(doc)).items
    assert parsed.last_message is not None and parsed.last_message.outbound


def test_more_than_two_participants_is_a_group_even_without_the_flag() -> None:
    conv = _with(mp.ONE_TO_ONE_INBOUND, others=(mp.ZEPHYRINE, mp.THADDEUS))
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([conv])).items
    assert item.kind is Kind.GROUP


def test_a_message_with_no_actor_reads_its_sender() -> None:
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))
    [item] = _field_items(doc)
    actorless = mp.Msg(11, 3, mp.ZEPHYRINE, "Invented.", mp.T0, actorless=True)
    item["messages"]["elements"] = [mp.message(actorless)]
    assert item["messages"]["elements"][0]["actor"] is None

    [parsed] = shapes.parse_conversation_list(json.dumps(doc)).items
    assert parsed.last_message is not None
    assert parsed.last_message.sender_urn == mp.ZEPHYRINE.urn


def test_a_long_message_is_cut_and_never_in_a_repr() -> None:
    long = mp.Msg(11, 3, mp.ZEPHYRINE, "Lorem " * 100, mp.T0)
    conv = _with(mp.ONE_TO_ONE_INBOUND, last=long)
    [item] = shapes.parse_conversation_list(mp.conversations_by_sync_token([conv])).items
    assert item.last_message is not None
    assert len(item.last_message.text_snippet) == SNIPPET_MAX
    assert "Lorem" not in repr(item.last_message)


def test_a_body_with_no_text_is_an_empty_snippet_not_a_refusal() -> None:
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))
    [item] = _field_items(doc)
    item["messages"]["elements"][0]["body"] = None
    [parsed] = shapes.parse_conversation_list(json.dumps(doc)).items
    assert parsed.last_message is not None and parsed.last_message.text_snippet == ""


# --- a thread ----------------------------------------------------------------------------


def test_a_thread_comes_back_oldest_first_with_directions() -> None:
    body = mp.messages_by_sync_token(mp.THREAD_ONE_TO_ONE)
    answer = shapes.parse_thread(
        body, owner_urn=mp.OWNER.urn, conversation_urn=mp.conversation_urn(11)
    )

    assert [m.at for m in answer.messages] == sorted(m.at for m in answer.messages)
    assert [m.outbound for m in answer.messages] == [True, False, False]
    assert answer.messages[0].sender_urn == mp.OWNER.urn


def test_a_thread_message_of_another_conversation_refuses_the_whole_answer() -> None:
    body = mp.messages_by_sync_token(mp.THREAD_ONE_TO_ONE)
    with pytest.raises(RouteChanged):
        shapes.parse_thread(body, owner_urn=mp.OWNER.urn, conversation_urn=mp.conversation_urn(12))


def test_a_list_answer_is_not_a_thread_answer_and_back() -> None:
    with pytest.raises(RouteChanged):
        shapes.parse_thread(
            mp.conversations_by_sync_token(mp.INBOX_FIRST_PAGE),
            owner_urn=mp.OWNER.urn,
            conversation_urn=None,
        )
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(mp.messages_by_sync_token(mp.THREAD_ONE_TO_ONE))


# --- an unknown shape refuses, loudly ---------------------------------------------------


def _drop(path: tuple[str | int, ...]) -> Callable[[dict[str, Any]], None]:
    def mutate(doc: dict[str, Any]) -> None:
        node: Any = doc
        for step in path[:-1]:
            node = node[step]
        del node[path[-1]]

    return mutate


def _set(path: tuple[str | int, ...], value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(doc: dict[str, Any]) -> None:
        node: Any = doc
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = value

    return mutate


_ITEM = ("data", mp.BY_SYNC_TOKEN, "elements", 0)
_MSG = (*_ITEM, "messages", "elements", 0)

BAD_LISTS: dict[str, Callable[[dict[str, Any]], None]] = {
    "no entityUrn": _drop((*_ITEM, "entityUrn")),
    "urn not a conversation urn": _set((*_ITEM, "entityUrn"), "urn:li:something:else"),
    "no lastActivityAt": _drop((*_ITEM, "lastActivityAt")),
    "lastActivityAt a string": _set((*_ITEM, "lastActivityAt"), "yesterday"),
    "lastActivityAt a bool": _set((*_ITEM, "lastActivityAt"), True),
    "no groupChat": _drop((*_ITEM, "groupChat")),
    "groupChat a string": _set((*_ITEM, "groupChat"), "false"),
    "no categories": _drop((*_ITEM, "categories")),
    "no participants": _drop((*_ITEM, "conversationParticipants")),
    "participants not a list": _set((*_ITEM, "conversationParticipants"), {}),
    "no SELF participant": _set(
        (*_ITEM, "conversationParticipants", 0, "participantType"), {"member": None}
    ),
    "no conversationUrl": _drop((*_ITEM, "conversationUrl")),
    "conversationUrl another thread": _set(
        (*_ITEM, "conversationUrl"), "https://www.linkedin.com/messaging/thread/2-other/"
    ),
    "no message urn": _drop((*_MSG, "entityUrn")),
    "no sender": _drop((*_MSG, "sender")),
    "sender not a profile": _set((*_MSG, "sender", "hostIdentityUrn"), "urn:li:fsd_company:1"),
    "no deliveredAt": _drop((*_MSG, "deliveredAt")),
    "deliveredAt a string": _set((*_MSG, "deliveredAt"), "now"),
    "no body key": _drop((*_MSG, "body")),
    "no originToken key": _drop((*_MSG, "originToken")),
    "two messages in the list": lambda d: _field_items(d)[0]["messages"]["elements"].append(
        deepcopy(_field_items(d)[0]["messages"]["elements"][0])
    ),
    "a stranger's message with an originToken": _set((*_MSG, "originToken"), "invented-token"),
    "no elements": _drop(("data", mp.BY_SYNC_TOKEN, "elements")),
    "elements not a list": _set(("data", mp.BY_SYNC_TOKEN, "elements"), {}),
    "counterpart neither profile nor company": _set(
        (*_ITEM, "conversationParticipants", 1, "hostIdentityUrn"), "urn:li:something:1"
    ),
}


@pytest.mark.parametrize("name", sorted(BAD_LISTS))
def test_a_list_that_does_not_parse_is_refused_whole(name: str) -> None:
    doc = _doc(mp.conversations_by_sync_token(mp.INBOX_FIRST_PAGE))
    BAD_LISTS[name](doc)
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))


def test_one_bad_item_among_good_ones_hands_nothing_on() -> None:
    doc = _doc(mp.conversations_by_sync_token(mp.INBOX_FIRST_PAGE))
    del _field_items(doc)[3]["lastActivityAt"]
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))


def test_two_mailbox_owners_in_one_answer_are_refused() -> None:
    doc = _doc(mp.conversations_by_sync_token(mp.INBOX_FIRST_PAGE))
    other = mp.Member(2, "Other", "Owner", "Invented", "SELF")
    urn = _field_items(doc)[0]["entityUrn"].replace(mp.OWNER.urn, other.urn)
    _field_items(doc)[0]["entityUrn"] = urn
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))


def test_the_urn_owner_must_be_the_self_participant() -> None:
    doc = _doc(mp.conversations_by_sync_token([mp.ONE_TO_ONE_INBOUND]))
    [item] = _field_items(doc)
    item["entityUrn"] = mp.conversation_urn(11, mp.ZEPHYRINE)
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))


@pytest.mark.parametrize("body", ["", "not json", "[]", "{}", '{"data": 1}', '{"data": {}}'])
def test_a_body_that_is_not_an_answer_never_reads_as_an_empty_inbox(body: str) -> None:
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(body)


def test_a_category_answer_needs_its_cursor_key() -> None:
    doc = _doc(mp.conversations_by_category(mp.INBOX_OLDER_PAGE, next_cursor=None))
    del doc["data"][mp.BY_CATEGORY]["metadata"]["nextCursor"]
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps(doc))
    with pytest.raises(RouteChanged):
        shapes.parse_conversation_list(json.dumps({"data": {mp.BY_CATEGORY: {"elements": []}}}))


def test_the_end_of_the_list_is_a_missing_cursor() -> None:
    answer = shapes.parse_conversation_list(
        mp.conversations_by_category(mp.INBOX_OLDER_PAGE, next_cursor=None)
    )
    assert answer.next_cursor is None


# --- the request ------------------------------------------------------------------------


def test_the_request_helpers_read_the_query_name_and_variables() -> None:
    sync = mp.conversations_sync_url()
    assert shapes.query_name(sync) == "messengerConversations"
    assert shapes.request_mailbox(sync) == mp.OWNER.urn
    assert not shapes.request_has_sync_token(sync)
    assert shapes.request_has_sync_token(mp.conversations_sync_url("invented-token"))
    first_older = mp.conversations_category_url(last_updated_before=mp.T0)
    assert shapes.request_last_updated_before(first_older) == mp.T0
    assert shapes.request_next_cursor(first_older) is None
    later = mp.conversations_category_url(next_cursor="abc=/def")
    assert shapes.request_next_cursor(later) == "abc=/def"
    thread = mp.messages_sync_url(11)
    assert shapes.query_name(thread) == "messengerMessages"
    assert shapes.request_conversation(thread) == mp.conversation_urn(11)
    assert shapes.query_name("https://www.linkedin.com/voyager/api/x") is None
