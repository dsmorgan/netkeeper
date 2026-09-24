"""Contact info as a source seam: the in-page API first, the overlay DOM second (spec 9.3, 9.4).

The counterpart to :mod:`netkeeper.linkedin.connections`'s ``ConnectionsSource``,
for the other data source spec 9.3 names: "the connections page infinite scroll
and the contact-info overlay". Enrichment (P2-07, #103) is being built in a
separate worktree while this module is written and fetches contact info today
through the in-page Voyager API
(:data:`~netkeeper.linkedin.voyager.CONTACT_INFO_PATH_TEMPLATE`,
:func:`~netkeeper.linkedin.voyager.parse_contact_info`) directly, with no seam
of its own -- so nothing here is wired into ``linkedin/enrich.py`` or its
runner yet. This module is what P2-07's job (or whichever of the two PRs
merges second) can adopt in place of that direct call: construct a
:class:`FallbackContactInfoSource` from the same :class:`VoyagerFetch
<netkeeper.linkedin.voyager.VoyagerFetch>` and :class:`BrowserRun
<netkeeper.linkedin.browser.BrowserRun>` the job already holds, and call
:meth:`ContactInfoSource.fetch_contact_info` where it used to call the
Voyager path directly. See this item's PR body for the fuller note.

This module stays pure, exactly like :mod:`netkeeper.linkedin.connections`:
:class:`ContactInfoSource` and :class:`ApiContactInfoSource` need nothing but
a :class:`~netkeeper.linkedin.voyager.VoyagerFetch`, never a
:class:`~netkeeper.linkedin.browser.BrowserRun` -- so this file, like
``connections.py``, is not a browser module and does not appear in
``tests/test_browser_safety.py``'s ``BROWSER_MODULES``.
:mod:`netkeeper.linkedin.dom`'s ``DomContactInfoSource`` is the DOM
implementation of the same protocol (spec 9.3: "linkedin/dom.py holds the
fallback for the two paths that matter most"); it lives there, alongside
``DomConnectionsSource``, because it is the one half of this seam that
actually touches a browser.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.voyager import (
    CONTACT_INFO_ENDPOINT,
    ContactInfo,
    RouteChanged,
    VoyagerFetch,
    VoyagerRequest,
    contact_info_path,
    parse_contact_info,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContactInfoResult:
    """What one source answered for one profile: the classification, and the info when ``Ok``.

    ``info`` is ``None`` for every outcome but ``Ok``, and for an ``Ok``
    response whose body did not parse (then ``outcome`` is ``RouteChanged``) --
    the same shape :class:`~netkeeper.linkedin.connections.SourcePage` uses for
    connections pages, so both source seams answer "was this ok, and if so,
    here is the data" the same way.
    """

    outcome: Outcome
    final_url: str
    info: ContactInfo | None = None


class ContactInfoSource(Protocol):
    """Where one profile's contact info comes from.

    Implementations: :class:`ApiContactInfoSource` (the in-page API, spec 9.3)
    and :mod:`netkeeper.linkedin.dom`'s ``DomContactInfoSource`` (the overlay
    DOM, P2-08).
    """

    @property
    def endpoint(self) -> str:
        """A name for logs and ``RouteChanged``: which endpoint this reads."""
        ...

    async def fetch_contact_info(self, public_id: str) -> ContactInfoResult:
        """One profile's contact info, classified, and parsed only when ``Ok``."""
        ...


@dataclass(frozen=True, slots=True)
class ApiContactInfoSource:
    """The in-page Voyager API as a :class:`ContactInfoSource` (spec 9.3, 9.4 step 3).

    ``fetch`` is the :class:`~netkeeper.linkedin.voyager.VoyagerFetch` the
    browser side provides, exactly as
    :class:`~netkeeper.linkedin.connections.VoyagerConnections` takes one for
    the connections list -- this class needs no ``BrowserRun`` of its own.
    """

    fetch: VoyagerFetch

    @property
    def endpoint(self) -> str:
        return CONTACT_INFO_ENDPOINT

    async def fetch_contact_info(self, public_id: str) -> ContactInfoResult:
        request = VoyagerRequest(path=contact_info_path(public_id))
        response = await self.fetch(request)
        outcome = classify(response.status, response.final_url, response.body)
        if outcome is not Outcome.OK:
            # Never parsed: a checkpoint or login-wall body is not contact info
            # (mirrors VoyagerConnections.fetch_page's own gate, spec 9.7).
            return ContactInfoResult(outcome=outcome, final_url=response.final_url)
        try:
            info = parse_contact_info(response.body)
        except RouteChanged:
            return ContactInfoResult(outcome=Outcome.ROUTE_CHANGED, final_url=response.final_url)
        return ContactInfoResult(outcome=Outcome.OK, final_url=response.final_url, info=info)


@dataclass(slots=True)
class FallbackContactInfoSource:
    """A :class:`ContactInfoSource` over two others: the API first, DOM second (spec 9.3).

    The contact-info counterpart of
    :class:`~netkeeper.linkedin.connections.FallbackConnectionsSource`, with
    the identical one-way switch and the identical reasoning for why it
    cannot loop or double-spend budget -- see that class's docstring; nothing
    here differs except that a "unit of work" is one profile's contact info
    rather than one page of the connections list, so there is no page-level
    ``count``/``start`` to keep consistent across the switch, only a
    ``public_id`` per call.

    Once ``primary`` answers ``RouteChanged`` for any profile, every
    subsequent call -- for that profile and every later one -- goes to
    ``fallback`` instead, for the rest of this source's life. A single
    instance is for one enrichment run, the same as
    ``FallbackConnectionsSource`` is for one sync run.
    """

    primary: ContactInfoSource
    fallback: ContactInfoSource
    _switched: bool = field(default=False, init=False, repr=False)

    @property
    def endpoint(self) -> str:
        return self.fallback.endpoint if self._switched else self.primary.endpoint

    async def fetch_contact_info(self, public_id: str) -> ContactInfoResult:
        if not self._switched:
            answer = await self.primary.fetch_contact_info(public_id)
            if answer.outcome is not Outcome.ROUTE_CHANGED:
                return answer
            log.warning(
                "contact info: %s route changed; falling back to %s for the rest of this run",
                self.primary.endpoint,
                self.fallback.endpoint,
            )
            self._switched = True
        return await self.fallback.fetch_contact_info(public_id)
