"""P2-14: the extractor boundary, enforced (spec 9.10, ADR 0005).

Three rules, three mechanisms:

* nothing under ``netkeeper/linkedin/`` imports the models, the session, the
  scoping helper, the CRM, or SQLAlchemy -- written down or dragged in. That is
  ``tests/test_browser_safety.py``'s syntax-tree scan and per-module subprocess
  sweep, both reading ``tests/boundary.py``'s one list; it is not repeated here;
* the job speaks only in plain dataclasses: a spec in, pages and a result out,
  none of them able to carry a row, a session, or anything but data (below);
* one module turns extractor results into rows, ``netkeeper/crm/apply.py``
  (below). A second module that took a ``ConnectionsPage`` and wrote contacts
  would be a second place the edge lifecycle could drift.
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import types
import typing
from datetime import datetime
from pathlib import Path

import boundary
import call_targets
import test_browser_safety as browser_safety

from netkeeper.linkedin import connections, enrich
from netkeeper.models import Base

# --- the interface is plain data ---------------------------------------------------

#: Everything that crosses the boundary from the job's side (spec 9.10's table).
INTERFACE = (
    connections.SyncJobSpec,
    connections.ConnectionsPage,
    connections.ProgressEvent,
    connections.SyncResult,
    connections.SourcePage,
    enrich.EnrichJobSpec,
    enrich.ProfileHarvest,
    enrich.ProgressEvent,
    enrich.EnrichResult,
)

#: The leaf types an interface field may hold.
PLAIN = (str, int, float, bool, datetime, type(None))


def _leaves(annotation: object, seen: set[type]) -> list[str]:
    """Every leaf of ``annotation`` that is not plain data, by name."""
    origin = typing.get_origin(annotation)
    if origin in (tuple, frozenset, typing.Union, types.UnionType):
        return [
            bad
            for arg in typing.get_args(annotation)
            if arg is not Ellipsis
            for bad in _leaves(arg, seen)
        ]
    if origin is not None:
        return [f"{annotation!r} (only tuple and frozenset: immutable containers)"]
    if not isinstance(annotation, type):
        return [repr(annotation)]
    if annotation in PLAIN or issubclass(annotation, enum.StrEnum):
        return []
    if dataclasses.is_dataclass(annotation):
        return _bad_fields(annotation, seen)
    return [annotation.__qualname__]


def _bad_fields(cls: type, seen: set[type]) -> list[str]:
    if cls in seen:
        return []
    seen.add(cls)
    params = getattr(cls, "__dataclass_params__", None)
    problems = [] if params is not None and params.frozen else [f"{cls.__name__} is not frozen"]
    hints = typing.get_type_hints(cls)
    for field in dataclasses.fields(cls):
        problems += [
            f"{cls.__name__}.{field.name}: {bad}" for bad in _leaves(hints[field.name], seen)
        ]
    return problems


def test_everything_that_crosses_the_boundary_is_frozen_plain_data() -> None:
    problems = [bad for cls in INTERFACE for bad in _bad_fields(cls, set())]
    assert not problems, "spec 9.10: plain dataclasses across the boundary\n" + "\n".join(problems)


class _Row:  # stands in for an ORM model
    pass


@dataclasses.dataclass(frozen=True)
class _Leaky:
    row: _Row


@dataclasses.dataclass(frozen=True)
class _Mutable:
    urns: list[str]


@dataclasses.dataclass
class _Thawed:
    urn: str


def test_the_plain_data_check_catches_a_row_a_list_and_a_mutable_class() -> None:
    """The check above, shown a field it must refuse, refuses it."""
    assert _bad_fields(_Leaky, set()) == ["_Leaky.row: _Row"]
    assert _bad_fields(_Mutable, set())
    assert _bad_fields(_Thawed, set()) == ["_Thawed is not frozen"]
    assert _bad_fields(connections.SyncResult, set()) == []


def test_the_job_takes_a_spec_and_returns_a_result() -> None:
    signature = inspect.signature(connections.run_connections_sync)
    hints = typing.get_type_hints(connections.run_connections_sync)
    first = next(iter(signature.parameters))
    assert hints[first] is connections.SyncJobSpec
    assert hints["return"] is connections.SyncResult


def test_the_enrichment_job_takes_a_spec_and_returns_a_result() -> None:
    signature = inspect.signature(enrich.run_enrichment)
    hints = typing.get_type_hints(enrich.run_enrichment)
    first = next(iter(signature.parameters))
    assert hints[first] is enrich.EnrichJobSpec
    assert hints["return"] is enrich.EnrichResult


# --- one module maps results onto rows ----------------------------------------------


def _model_names() -> set[str]:
    names: set[str] = set()
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        names.add(f"{cls.__module__}.{cls.__name__}")
        names.add(f"netkeeper.models.{cls.__name__}")
    return names


def _names_in(source: str, path: Path) -> set[str]:
    """What ``source``, as the file at ``path``, imports and reads, fully qualified."""
    module = call_targets.module_name(path, browser_safety.REPO_ROOT)
    imported = {name for _, name in browser_safety.imported_names(source, path)}
    read = call_targets.qualified_references(source, module, is_package=path.name == "__init__.py")
    return imported | set(read)


def _mapping_modules() -> set[str]:
    package = Path(browser_safety.PACKAGE)
    models = _model_names()
    found: set[str] = set()
    for path in browser_safety.python_files(package):
        if boundary.maps_results(_names_in(path.read_text(encoding="utf-8"), path), models):
            found.add(path.resolve().relative_to(browser_safety.REPO_ROOT).as_posix())
    return found


def test_crm_apply_is_the_only_module_that_maps_extractor_results_onto_rows() -> None:
    found = _mapping_modules()
    assert found == set(boundary.MAPPING_MODULES), (
        "spec 9.10: the core's crm/apply.py maps extractor results onto rows, and only it.\n"
        f"  modules that handle a result and name a table: {sorted(found)}\n"
        f"  allowed: {sorted(boundary.MAPPING_MODULES)}\n"
        "Move the mapping into crm/apply.py and call it from there."
    )


def test_the_mapping_scan_would_see_a_second_mapper() -> None:
    """A snippet that takes a page and writes a contact maps; one that passes it on does not."""
    models = _model_names()
    mapper = (
        "from netkeeper.linkedin.connections import ConnectionsPage\n"
        "from netkeeper.models import Contact\n"
        "def write(session, page: ConnectionsPage):\n"
        "    for c in page.connections:\n"
        "        session.add(Contact(li_urn=c.urn))\n"
    )
    relay = (
        "from netkeeper.crm import apply as mapping\n"
        "from netkeeper.linkedin.connections import ConnectionsPage\n"
        "from netkeeper.models import User\n"
        "def on_page(session, user: User, page: ConnectionsPage):\n"
        "    mapping.apply_page(session, user, page)\n"
    )

    def refs(source: str) -> set[str]:
        return _names_in(source, Path(browser_safety.PACKAGE) / "services" / "some_job.py")

    assert boundary.maps_results(refs(mapper), models)
    assert not boundary.maps_results(refs(relay), models)
