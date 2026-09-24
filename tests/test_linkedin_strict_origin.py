"""``netkeeper.linkedin.strict_origin``: closing the urlsplit-vs-browser gap (F3).

A reviewer found that ``rehearse._require_neutral`` and ``fetch._require_fetchable_origin``
both trusted ``urlsplit`` alone, and both could be fooled the same way: a backslash
before an ``@`` reads as loopback to Python and as LinkedIn to Chrome. Every test here
is a case that must be refused before ``urlsplit`` sees it, or must resolve to the one
host a real browser would actually reach.
"""

from __future__ import annotations

import pytest

from netkeeper.linkedin.strict_origin import NotAStrictOrigin, Origin, parse_strict_origin

# --- the attack this module exists to close -----------------------------------


@pytest.mark.parametrize(
    "value",
    [
        r"http://www.linkedin.com\@127.0.0.1:8080",
        r"http://evil.example\@127.0.0.1",
        r"https://www.linkedin.com\@127.0.0.1",
        r"http://127.0.0.1\@www.linkedin.com",
    ],
)
def test_a_backslash_before_userinfo_is_refused_outright(value: str) -> None:
    """The exact WHATWG-vs-urlsplit differential the reviewer demonstrated.

    Python's urlsplit would read every one of these as pointing at whichever host
    follows the last '@' -- the opposite of where a real browser, which folds '\\'
    into '/' for http(s), would actually go. Refusing on sight is the only fix that
    does not require trusting urlsplit's answer for the case that matters most.
    """
    with pytest.raises(NotAStrictOrigin, match="backslash"):
        parse_strict_origin(value)


def test_urlsplit_would_have_been_fooled_by_the_backslash_case() -> None:
    """Proof the vulnerability was real, not hypothetical -- pinned so it cannot regress."""
    from urllib.parse import urlsplit

    fooled = urlsplit(r"http://www.linkedin.com\@127.0.0.1:8080")
    assert fooled.hostname == "127.0.0.1", (
        "if this ever stops being true, the module docstring's claim needs updating"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://user@127.0.0.1",
        "http://user:pass@127.0.0.1",
        "https://www.linkedin.com@evil.example",
        "http://@127.0.0.1",
    ],
)
def test_userinfo_without_a_backslash_is_also_refused(value: str) -> None:
    with pytest.raises(NotAStrictOrigin, match="userinfo"):
        parse_strict_origin(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1/path",
        "http://127.0.0.1/../evil",
        "http://127.0.0.1?x=1",
        "http://127.0.0.1#frag",
        "http://127.0.0.1/path?x=1#frag",
    ],
)
def test_anything_past_the_authority_is_refused(value: str) -> None:
    with pytest.raises(NotAStrictOrigin, match=r"path|query|fragment"):
        parse_strict_origin(value)


def test_a_bare_trailing_slash_is_allowed() -> None:
    origin = parse_strict_origin("http://127.0.0.1:8080/")
    assert origin == Origin(scheme="http", host="127.0.0.1", port=8080)


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1 ",
        " http://127.0.0.1",
        "http://127.0.0.1\t",
        "http://127.0.0.1\n",
        "http://127.0.0.1\r",
        "http://127.0.0.1\x00",
        "http://127.0.0.1\x7f",
        "ht tp://127.0.0.1",
    ],
)
def test_whitespace_and_control_characters_are_refused(value: str) -> None:
    with pytest.raises(NotAStrictOrigin):
        parse_strict_origin(value)


@pytest.mark.parametrize("value", ["", "not-a-url", "://127.0.0.1", "http://"])
def test_unparseable_or_schemeless_or_hostless_strings_are_refused(value: str) -> None:
    with pytest.raises(NotAStrictOrigin):
        parse_strict_origin(value)


# --- what a valid origin rebuilds to --------------------------------------------


def test_a_plain_origin_round_trips() -> None:
    origin = parse_strict_origin("https://www.linkedin.com")
    assert origin == Origin(scheme="https", host="www.linkedin.com", port=None)
    assert str(origin) == "https://www.linkedin.com"


def test_a_port_is_kept_and_rebuilt() -> None:
    origin = parse_strict_origin("http://127.0.0.1:52341")
    assert origin.port == 52341
    assert str(origin) == "http://127.0.0.1:52341"


def test_an_ipv6_loopback_round_trips_with_its_brackets() -> None:
    origin = parse_strict_origin("http://[::1]:9999")
    assert origin.host == "::1"
    # The rebuilt form must still be a url a browser can parse: dropping the
    # brackets around an IPv6 literal would make ':' ambiguous with the port
    # separator, exactly the kind of self-inflicted parser confusion this module
    # exists to prevent.
    assert str(origin) == "http://[::1]:9999"


def test_hostname_is_lowercased_the_way_urlsplit_already_does() -> None:
    origin = parse_strict_origin("HTTP://WWW.LINKEDIN.COM")
    assert origin.host == "www.linkedin.com"
