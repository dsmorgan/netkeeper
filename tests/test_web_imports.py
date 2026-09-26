"""The imports API (P1-04, spec 14.1): inspect, draft, preview, commit, rollback, presets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import identity
from netkeeper.crm.importer import FIELD_SIZE_LIMIT
from netkeeper.crm.provenance import set_manual_field
from netkeeper.db import session_scope
from netkeeper.models import Contact, ContactSource, User
from netkeeper.scoping import scoped

CSRF = {"X-Netkeeper-Client": "1"}
FIXTURES = Path(__file__).parent / "fixtures" / "csv"
LINKEDHELPER = (FIXTURES / "linkedhelper-sample.csv").read_text()
NINE_COLUMN = (FIXTURES / "nine-column-sample.csv").read_text()

ROW_REFUSED = 2
ROW_CANDIDATE = 3


@pytest.fixture
def seeded(running_app: FastAPI) -> dict[str, int]:
    """The contacts the LinkedHelper sample should find, by id."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User)).one()
        fern = factories.make_contact(
            session,
            user,
            li_urn=None,
            li_public_id="fern-oglethorpe-qz",
            first_name="Fern",
            last_name="Oglethorpe",
            headline=None,
            current_title="Kite Apprentice",
            current_company="Brimstone Kite Works",
            source=ContactSource.CSV,
        )
        wilhelmina = factories.make_contact(
            session,
            user,
            li_urn=None,
            li_public_id="wilhelmina-pockrandt-qz",
            first_name="Wilhelmina",
            last_name="Pockrandt",
            headline="Tinsmith, retired",
            current_title="Chief Tinsmith",
            current_company="Gossamer Tin Ltd",
            source=ContactSource.CSV,
        )
        set_manual_field(wilhelmina, "headline", "Tinsmith, retired")
        barnaby = factories.make_contact(
            session,
            user,
            first_name="Barnaby",
            last_name="Fitzmaurice",
            current_company="Wobblegong Analytics",
            source=ContactSource.SYNC,
        )
        session.flush()
        return {"fern": fern.id, "wilhelmina": wilhelmina.id, "barnaby": barnaby.id}


async def draft(client: httpx.AsyncClient, content: str = LINKEDHELPER, **body: Any) -> Any:
    response = await client.post(
        "/api/v1/imports",
        headers=CSRF,
        json={"filename": "sample.csv", "content": content, **body},
    )
    assert response.status_code == 201, response.text
    return response.json()


def contact_by_slug(app: FastAPI, slug: str) -> Contact | None:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory) as session:
        user = session.scalars(select(User)).one()
        return session.scalars(
            scoped(user, Contact).where(Contact.li_public_id == slug)
        ).one_or_none()


# --- the guard --------------------------------------------------------------


async def test_state_changing_routes_need_the_csrf_header(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("POST", "/api/v1/imports"),
        ("POST", "/api/v1/imports/inspect"),
        ("POST", "/api/v1/imports/archive"),
        ("POST", "/api/v1/imports/1/preview"),
        ("POST", "/api/v1/imports/1/commit"),
        ("POST", "/api/v1/imports/1/rollback"),
        ("DELETE", "/api/v1/imports/1"),
        ("PUT", "/api/v1/imports/presets/ours"),
        ("DELETE", "/api/v1/imports/presets/ours"),
    ]:
        response = await client.request(method, path, json={})
        assert response.status_code == 403, f"{method} {path}: {response.status_code}"


# --- inspecting -------------------------------------------------------------


async def test_inspect_shows_the_columns_and_the_preset_that_fits(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/imports/inspect", headers=CSRF, json={"content": NINE_COLUMN}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["detected_preset"] == "nine-column"
    assert body["preset"] == "nine-column"
    assert body["mapping"]["Current Job Title"] == "current_title"
    assert body["row_count"] == 3
    assert body["unmapped"] == []
    assert body["sample"][0]["First Name"] == "Fern"


async def test_inspect_stores_nothing(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/imports/inspect", headers=CSRF, json={"content": NINE_COLUMN})
    listed = await client.get("/api/v1/imports")
    assert listed.json() == {"items": [], "total": 0}


async def test_a_file_with_no_header_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/imports/inspect", headers=CSRF, json={"content": ""})
    assert response.status_code == 422


async def test_a_field_past_the_readers_limit_is_a_bad_request_not_a_crash(
    client: httpx.AsyncClient,
) -> None:
    oversized = 'A,Headline\n1,"' + "x" * (FIELD_SIZE_LIMIT + 1024) + '"\n'
    inspected = await client.post(
        "/api/v1/imports/inspect", headers=CSRF, json={"content": oversized}
    )
    assert inspected.status_code == 422, inspected.status_code
    drafted = await client.post(
        "/api/v1/imports",
        headers=CSRF,
        json={"filename": "big.csv", "content": oversized},
    )
    assert drafted.status_code == 422, drafted.status_code


async def test_inspecting_a_file_no_preset_fits_shows_its_columns(
    client: httpx.AsyncClient,
) -> None:
    """Inspect writes nothing and exists to show the columns, so it never refuses.

    A CSV from anything but LinkedIn lands here, and refusing it left the only
    screen that can map it by hand out of reach.
    """
    response = await client.post(
        "/api/v1/imports/inspect",
        headers=CSRF,
        json={"content": "Widget,Sprocket\n1,2\n"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["headers"] == ["Widget", "Sprocket"]
    assert body["mapping"] == {}
    assert body["unmapped"] == ["Widget", "Sprocket"]
    assert body["preset"] is None
    assert body["detected_preset"] is None
    assert body["row_count"] == 1


async def test_inspecting_a_file_one_preset_half_fits_still_shows_every_column(
    client: httpx.AsyncClient,
) -> None:
    """Two columns is under MIN_PRESET_MATCH, so nothing is detected and nothing raises."""
    response = await client.post(
        "/api/v1/imports/inspect",
        headers=CSRF,
        json={"content": "First Name,Last Name\nAda,Pallisade\n"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["detected_preset"] is None
    assert body["mapping"] == {}
    assert body["unmapped"] == ["First Name", "Last Name"]


async def test_a_file_no_preset_fits_can_be_mapped_by_hand(client: httpx.AsyncClient) -> None:
    """The mapping the inspect screen collects is what makes the run readable."""
    content = "Widget,Sprocket\nAda,Pallisade\n"
    response = await client.post(
        "/api/v1/imports/inspect",
        headers=CSRF,
        json={"content": content, "mapping": {"Widget": "first_name", "Sprocket": "last_name"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["mapping"] == {"Widget": "first_name", "Sprocket": "last_name"}

    drafted = await client.post(
        "/api/v1/imports",
        headers=CSRF,
        json={
            "filename": "by-hand.csv",
            "content": content,
            "mapping": {"Widget": "first_name", "Sprocket": "last_name"},
        },
    )
    assert drafted.status_code == 201, drafted.text
    assert drafted.json()["total_rows"] == 1


async def test_a_run_still_needs_a_mapping_that_claims_a_column(
    client: httpx.AsyncClient,
) -> None:
    """Reading a file into a run is where the requirement lives now."""
    response = await client.post(
        "/api/v1/imports",
        headers=CSRF,
        json={"filename": "nothing.csv", "content": "Widget,Sprocket\n1,2\n"},
    )
    assert response.status_code == 422, response.text
    assert "no column is mapped" in response.json()["detail"]


# --- drafting and previewing ------------------------------------------------


async def test_a_draft_counts_the_whole_file(client: httpx.AsyncClient, seeded: object) -> None:
    body = await draft(client)
    assert body["status"] == "draft"
    assert body["preset"] == "linkedhelper"
    assert body["total_rows"] == 6
    assert (body["matched_count"], body["created_count"], body["candidate_count"]) == (3, 2, 1)


async def test_preview_names_what_would_change_and_what_is_refused(
    client: httpx.AsyncClient, seeded: dict[str, int]
) -> None:
    run = await draft(client)
    response = await client.post(f"/api/v1/imports/{run['id']}/preview", headers=CSRF)
    assert response.status_code == 200, response.text
    rows = response.json()

    assert [row["resolution"] for row in rows] == [
        "matched",
        "matched",
        "candidate",
        "created",
        "matched",
        "created",
    ]
    assert rows[0]["contact_id"] == seeded["fern"]
    assert rows[2]["candidate_ids"] == [seeded["barnaby"]]
    refused = [change for change in rows[ROW_REFUSED - 1]["changes"] if change["refused"]]
    assert [change["field"] for change in refused] == ["headline"]
    assert refused[0]["kept_source"] == "manual"
    assert refused[0]["before"] == "Tinsmith, retired"


async def test_preview_writes_nothing(
    client: httpx.AsyncClient, running_app: FastAPI, seeded: object
) -> None:
    run = await draft(client)
    await client.post(f"/api/v1/imports/{run['id']}/preview", headers=CSRF)
    assert contact_by_slug(running_app, "imogen-thistlewhite-qz") is None
    still_draft = await client.get(f"/api/v1/imports/{run['id']}")
    assert still_draft.json()["status"] == "draft"


async def test_preview_takes_a_limit(client: httpx.AsyncClient, seeded: object) -> None:
    run = await draft(client)
    response = await client.post(
        f"/api/v1/imports/{run['id']}/preview", headers=CSRF, params={"limit": 2}
    )
    assert len(response.json()) == 2


# --- committing -------------------------------------------------------------


async def test_a_commit_stops_while_a_candidate_is_undecided(
    client: httpx.AsyncClient, seeded: object
) -> None:
    run = await draft(client)
    response = await client.post(f"/api/v1/imports/{run['id']}/commit", headers=CSRF, json={})
    assert response.status_code == 409
    assert "decision" in response.json()["detail"]
    still_draft = await client.get(f"/api/v1/imports/{run['id']}")
    assert still_draft.json()["status"] == "draft"


async def test_a_commit_applies_the_decisions_it_is_given(
    client: httpx.AsyncClient, running_app: FastAPI, seeded: dict[str, int]
) -> None:
    run = await draft(client)
    response = await client.post(
        f"/api/v1/imports/{run['id']}/commit",
        headers=CSRF,
        json={
            "decisions": [
                {
                    "row_number": ROW_CANDIDATE,
                    "kind": "merge_into",
                    "contact_id": seeded["barnaby"],
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "committed"
    assert body["matched_count"] == 4
    assert body["created_count"] == 2
    assert body["skipped_count"] == 0

    fern = contact_by_slug(running_app, "fern-oglethorpe-qz")
    assert fern is not None and fern.current_title == "Head of Kites"
    assert contact_by_slug(running_app, "imogen-thistlewhite-qz") is not None


async def test_a_merge_decision_without_a_contact_is_a_bad_request(
    client: httpx.AsyncClient, seeded: object
) -> None:
    run = await draft(client)
    response = await client.post(
        f"/api/v1/imports/{run['id']}/commit",
        headers=CSRF,
        json={"decisions": [{"row_number": ROW_CANDIDATE, "kind": "merge_into"}]},
    )
    assert response.status_code == 422


async def test_a_decision_on_a_row_the_run_does_not_have_is_a_bad_request(
    client: httpx.AsyncClient, seeded: object
) -> None:
    run = await draft(client)
    response = await client.post(
        f"/api/v1/imports/{run['id']}/commit",
        headers=CSRF,
        json={"decisions": [{"row_number": 99, "kind": "create_new"}]},
    )
    assert response.status_code == 422


async def test_the_rows_of_a_committed_run_carry_what_was_refused(
    client: httpx.AsyncClient, seeded: object
) -> None:
    run = await draft(client)
    await client.post(
        f"/api/v1/imports/{run['id']}/commit", headers=CSRF, json={"skip_undecided": True}
    )
    response = await client.get(f"/api/v1/imports/{run['id']}/rows")
    assert response.status_code == 200
    rows = {row["row_number"]: row for row in response.json()["items"]}

    assert rows[ROW_REFUSED]["refused"][0]["field"] == "headline"
    assert rows[ROW_REFUSED]["refused"][0]["source"] == "manual"
    assert rows[ROW_CANDIDATE]["resolution"] == "skipped"
    assert "waiting for a decision" in rows[ROW_CANDIDATE]["error"]


async def test_rows_can_be_filtered_by_resolution(
    client: httpx.AsyncClient, seeded: object
) -> None:
    run = await draft(client)
    response = await client.get(
        f"/api/v1/imports/{run['id']}/rows", params={"resolution": "candidate"}
    )
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["row_number"] == ROW_CANDIDATE


async def test_a_run_cannot_be_committed_twice(client: httpx.AsyncClient, seeded: object) -> None:
    run = await draft(client)
    first = await client.post(
        f"/api/v1/imports/{run['id']}/commit", headers=CSRF, json={"skip_undecided": True}
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/imports/{run['id']}/commit", headers=CSRF, json={"skip_undecided": True}
    )
    assert second.status_code == 409


# --- rolling back -----------------------------------------------------------


async def test_rollback_removes_only_what_the_run_created(
    client: httpx.AsyncClient, running_app: FastAPI, seeded: dict[str, int]
) -> None:
    run = await draft(client)
    await client.post(
        f"/api/v1/imports/{run['id']}/commit", headers=CSRF, json={"skip_undecided": True}
    )
    assert contact_by_slug(running_app, "imogen-thistlewhite-qz") is not None

    response = await client.post(f"/api/v1/imports/{run['id']}/rollback", headers=CSRF)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contacts_deleted"] == 2
    assert body["contacts_restored"] == 2

    assert contact_by_slug(running_app, "imogen-thistlewhite-qz") is None
    fern = contact_by_slug(running_app, "fern-oglethorpe-qz")
    assert fern is not None
    assert fern.id == seeded["fern"]
    assert fern.current_title == "Kite Apprentice"
    assert fern.headline is None
    wilhelmina = contact_by_slug(running_app, "wilhelmina-pockrandt-qz")
    assert wilhelmina is not None and wilhelmina.headline == "Tinsmith, retired"
    assert (await client.get(f"/api/v1/imports/{run['id']}")).json()["status"] == "rolled_back"


async def test_a_rollback_across_a_merge_is_refused(
    client: httpx.AsyncClient, running_app: FastAPI, seeded: object
) -> None:
    run = await draft(client)
    await client.post(
        f"/api/v1/imports/{run['id']}/commit", headers=CSRF, json={"skip_undecided": True}
    )
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User)).one()
        created = session.scalars(
            scoped(user, Contact).where(Contact.li_public_id == "imogen-thistlewhite-qz")
        ).one()
        older = factories.make_contact(
            session, user, li_urn=None, li_public_id=None, first_name="Imogen"
        )
        identity.merge(session, user, created.id, older.id)

    response = await client.post(f"/api/v1/imports/{run['id']}/rollback", headers=CSRF)

    assert response.status_code == 409
    body = response.json()
    assert "merge" in body["detail"]
    assert body["code"] == "merged"
    assert contact_by_slug(running_app, "imogen-thistlewhite-qz") is not None
    assert (await client.get(f"/api/v1/imports/{run['id']}")).json()["status"] == "committed"


async def test_rolling_back_a_run_a_later_one_wrote_over_names_the_later_run(
    client: httpx.AsyncClient, seeded: dict[str, int]
) -> None:
    """#78 item 1: refused with ``superseded`` and the runs to roll back first."""
    first = await draft(client)
    await client.post(
        f"/api/v1/imports/{first['id']}/commit", headers=CSRF, json={"skip_undecided": True}
    )
    later_file = (
        "Profile Url,First Name,Last Name,Position\n"
        "https://www.linkedin.com/in/fern-oglethorpe-qz/,Fern,Oglethorpe,Kite Director\n"
    )
    second = await draft(client, content=later_file)
    await client.post(f"/api/v1/imports/{second['id']}/commit", headers=CSRF, json={})

    response = await client.post(f"/api/v1/imports/{first['id']}/rollback", headers=CSRF)
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "superseded"
    assert body["run_ids"] == [second["id"]]
    assert body["contact_ids"] == [seeded["fern"]]
    # force never overrides this one: it would strand a value.
    forced = await client.post(
        f"/api/v1/imports/{first['id']}/rollback", headers=CSRF, params={"force": "true"}
    )
    assert forced.status_code == 409
    assert forced.json()["code"] == "superseded"


async def test_rolling_back_a_created_contact_that_gained_things_needs_force(
    client: httpx.AsyncClient, running_app: FastAPI, seeded: object
) -> None:
    """#78 item 2: say what would be lost; ``force`` deletes it anyway."""
    run = await draft(client)
    await client.post(
        f"/api/v1/imports/{run['id']}/commit", headers=CSRF, json={"skip_undecided": True}
    )
    imogen = contact_by_slug(running_app, "imogen-thistlewhite-qz")
    assert imogen is not None
    note = await client.post(
        f"/api/v1/contacts/{imogen.id}/interactions",
        headers=CSRF,
        json={"kind": "note", "at": "2026-09-01T12:00:00Z", "summary": "coffee"},
    )
    assert note.status_code == 201, note.text

    refused = await client.post(f"/api/v1/imports/{run['id']}/rollback", headers=CSRF)
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "created_contacts_changed"
    assert imogen.id in body["contact_ids"]
    assert body["acquired"]["interactions"] == 1
    assert "1 interaction" in body["detail"]
    assert contact_by_slug(running_app, "imogen-thistlewhite-qz") is not None

    forced = await client.post(
        f"/api/v1/imports/{run['id']}/rollback", headers=CSRF, params={"force": "true"}
    )
    assert forced.status_code == 200, forced.text
    assert contact_by_slug(running_app, "imogen-thistlewhite-qz") is None


async def test_a_saved_preset_maps_a_later_file_missing_one_of_its_columns(
    client: httpx.AsyncClient,
) -> None:
    await client.put(
        "/api/v1/imports/presets/our-crm",
        headers=CSRF,
        json={
            "mapping": {
                "Given": "first_name",
                "Family": "last_name",
                "Works At": "current_company",
            }
        },
    )
    run = await draft(client, content="Given,Family\nHortensia,Blennerhassett\n", preset="our-crm")
    assert run["mapping"] == {"Given": "first_name", "Family": "last_name"}
    assert run["created_count"] == 1


async def test_a_draft_cannot_be_rolled_back(client: httpx.AsyncClient) -> None:
    run = await draft(client, content=NINE_COLUMN)
    response = await client.post(f"/api/v1/imports/{run['id']}/rollback", headers=CSRF)
    assert response.status_code == 409


async def test_an_unknown_run_is_not_found(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/imports/404")).status_code == 404
    assert (await client.get("/api/v1/imports/404/rows")).status_code == 404
    rolled = await client.post("/api/v1/imports/404/rollback", headers=CSRF)
    assert rolled.status_code == 404


# --- presets ----------------------------------------------------------------


async def test_the_built_in_presets_are_listed(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/imports/presets")
    assert response.status_code == 200
    body = response.json()
    assert {preset["name"] for preset in body["builtin"]} == {
        "linkedin-archive",
        "linkedhelper",
        "nine-column",
    }
    assert all(preset["builtin"] for preset in body["builtin"])
    assert body["saved"] == []


async def test_a_mapping_can_be_saved_as_a_preset_and_used(client: httpx.AsyncClient) -> None:
    saved = await client.put(
        "/api/v1/imports/presets/our-crm",
        headers=CSRF,
        json={
            "mapping": {
                "Given": "first_name",
                "Family": "last_name",
                "Works At": "current_company",
            }
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "name": "our-crm",
        "builtin": False,
        "mapping": {
            "Given": "first_name",
            "Family": "last_name",
            "Works At": "current_company",
        },
    }
    listed = await client.get("/api/v1/imports/presets")
    assert [preset["name"] for preset in listed.json()["saved"]] == ["our-crm"]

    run = await draft(
        client,
        content="Given,Family,Works At\nHortensia,Blennerhassett,Tarnish and Sons\n",
        preset="our-crm",
    )
    assert run["preset"] == "our-crm"
    assert run["created_count"] == 1

    deleted = await client.delete("/api/v1/imports/presets/our-crm", headers=CSRF)
    assert deleted.status_code == 204
    assert (await client.get("/api/v1/imports/presets")).json()["saved"] == []


async def test_a_saved_preset_may_not_shadow_a_built_in_one(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/api/v1/imports/presets/nine-column",
        headers=CSRF,
        json={"mapping": {"A": "first_name"}},
    )
    assert response.status_code == 409


async def test_deleting_a_preset_nobody_saved_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.delete("/api/v1/imports/presets/mystery", headers=CSRF)
    assert response.status_code == 404


async def test_a_preset_naming_a_field_an_import_cannot_write_is_refused(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/v1/imports/presets/ours", headers=CSRF, json={"mapping": {"A": "notes"}}
    )
    assert response.status_code == 422


# --- listing ----------------------------------------------------------------


async def test_runs_are_listed_newest_first(client: httpx.AsyncClient) -> None:
    first = await draft(client, content=NINE_COLUMN)
    second = await draft(client, content=NINE_COLUMN)
    response = await client.get("/api/v1/imports")
    body = response.json()

    assert body["total"] == 2
    assert [run["id"] for run in body["items"]] == [second["id"], first["id"]]


async def test_runs_can_be_filtered_by_status(
    client: httpx.AsyncClient, seeded: dict[str, int]
) -> None:
    """``status=draft`` is how an orphaned run left by a dry run or a refused commit is found."""
    draft_run = await draft(client, content=NINE_COLUMN)
    committed_run = await draft(client)
    await client.post(
        f"/api/v1/imports/{committed_run['id']}/commit", headers=CSRF, json={"skip_undecided": True}
    )

    drafts = await client.get("/api/v1/imports", params={"status": "draft"})
    assert drafts.status_code == 200, drafts.text
    body = drafts.json()
    assert body["total"] == 1
    assert [run["id"] for run in body["items"]] == [draft_run["id"]]

    committed = await client.get("/api/v1/imports", params={"status": "committed"})
    assert [run["id"] for run in committed.json()["items"]] == [committed_run["id"]]


# --- deleting a draft (#90) ---------------------------------------------------


async def test_deleting_a_draft_removes_it_and_its_rows(client: httpx.AsyncClient) -> None:
    run = await draft(client, content=NINE_COLUMN)

    response = await client.delete(f"/api/v1/imports/{run['id']}", headers=CSRF)
    assert response.status_code == 204, response.text

    assert (await client.get(f"/api/v1/imports/{run['id']}")).status_code == 404
    assert (await client.get(f"/api/v1/imports/{run['id']}/rows")).status_code == 404
    listed = await client.get("/api/v1/imports")
    assert listed.json() == {"items": [], "total": 0}


async def test_deleting_a_committed_run_is_refused(
    client: httpx.AsyncClient, running_app: FastAPI, seeded: dict[str, int]
) -> None:
    run = await draft(client)
    await client.post(
        f"/api/v1/imports/{run['id']}/commit", headers=CSRF, json={"skip_undecided": True}
    )

    response = await client.delete(f"/api/v1/imports/{run['id']}", headers=CSRF)
    assert response.status_code == 409, response.text

    # Refused, not half-deleted: the run and the contact it made are both still there.
    assert (await client.get(f"/api/v1/imports/{run['id']}")).status_code == 200
    assert contact_by_slug(running_app, "imogen-thistlewhite-qz") is not None


async def test_deleting_a_rolled_back_run_is_refused(client: httpx.AsyncClient) -> None:
    run = await draft(client, content=NINE_COLUMN)
    await client.post(f"/api/v1/imports/{run['id']}/commit", headers=CSRF, json={})
    await client.post(f"/api/v1/imports/{run['id']}/rollback", headers=CSRF)

    response = await client.delete(f"/api/v1/imports/{run['id']}", headers=CSRF)
    assert response.status_code == 409, response.text


async def test_deleting_an_unknown_run_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.delete("/api/v1/imports/404", headers=CSRF)
    assert response.status_code == 404
