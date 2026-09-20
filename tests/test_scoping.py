import logging

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.base import Executable

from netkeeper.db import make_session_factory
from netkeeper.models import SettingKV, User, UserKind
from netkeeper.scoping import (
    SCOPE_OPTION,
    UNSCOPED_OPTION,
    UnscopedQueryError,
    get_scoped,
    install_scope_guard,
    owned_models_in,
    scoped,
    scoped_delete,
    scoped_update,
    unscoped,
)
from netkeeper.services.settings_kv import delete_setting, get_setting, set_setting


@pytest.fixture
def users(session: Session) -> tuple[User, User]:
    """Two users, each with one setting under the same key."""
    alice = User(kind=UserKind.LOCAL, display_name="alice")
    bob = User(kind=UserKind.HOSTED, display_name="bob")
    session.add_all([alice, bob])
    session.flush()
    session.add_all(
        [
            SettingKV(user_id=alice.id, key="theme", value="light"),
            SettingKV(user_id=bob.id, key="theme", value="dark"),
        ]
    )
    session.flush()
    return alice, bob


# --- the guard rejects unscoped statements ----------------------------------

UNSCOPED_STATEMENTS = {
    "select entity": select(SettingKV),
    "select column": select(SettingKV.key),
    "count": select(func.count()).select_from(SettingKV),
    "join as secondary entity": select(User).join(SettingKV),
    "outer join": select(User).outerjoin(SettingKV),
    "in subquery": select(User).where(User.id.in_(select(SettingKV.user_id))),
    "correlated exists": select(User).where(
        select(SettingKV).where(SettingKV.user_id == User.id).exists()
    ),
    "from subquery": select(select(SettingKV).subquery()),
    "alias": select(aliased(SettingKV)),
    "core select": select(SettingKV.__table__),
    "update": update(SettingKV).values(value=1),
    "delete": delete(SettingKV),
    "filtered by user_id but not marked": select(SettingKV).where(SettingKV.user_id == 1),
}


@pytest.mark.parametrize("statement", UNSCOPED_STATEMENTS.values(), ids=list(UNSCOPED_STATEMENTS))
def test_unscoped_statements_raise(
    session: Session, users: tuple[User, User], statement: Executable
) -> None:
    with pytest.raises(UnscopedQueryError, match="SettingKV"):
        session.execute(statement)


def test_scalars_and_scalar_go_through_the_guard(
    session: Session, users: tuple[User, User]
) -> None:
    with pytest.raises(UnscopedQueryError):
        session.scalars(select(SettingKV))
    with pytest.raises(UnscopedQueryError):
        session.scalar(select(func.count()).select_from(SettingKV))


def test_session_get_on_an_owned_model_raises(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    row_id = session.scalars(scoped(alice, SettingKV)).one().id
    session.expunge_all()  # otherwise get() answers from the identity map without a query
    with pytest.raises(UnscopedQueryError, match="get_scoped"):
        session.get(SettingKV, row_id)


def test_session_get_on_user_passes(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    session.expunge_all()
    assert session.get(User, alice.id) is not None


def test_error_names_every_owned_entity(session: Session, users: tuple[User, User]) -> None:
    with pytest.raises(UnscopedQueryError, match="unscoped select touching SettingKV"):
        session.execute(select(User).join(SettingKV))
    with pytest.raises(UnscopedQueryError, match="unscoped update touching SettingKV"):
        session.execute(update(SettingKV).values(value=1))
    with pytest.raises(UnscopedQueryError, match="unscoped delete touching SettingKV"):
        session.execute(delete(SettingKV))


# --- the scoped forms pass and filter ---------------------------------------


def test_scoped_select_returns_only_the_users_rows(
    session: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    assert [s.value for s in session.scalars(scoped(alice, SettingKV))] == ["light"]
    assert [s.value for s in session.scalars(scoped(bob, SettingKV))] == ["dark"]


def test_scoped_update_and_delete_touch_only_the_users_rows(
    session: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    session.execute(scoped_update(alice, SettingKV).values(value="solar"))
    assert session.scalars(scoped(alice, SettingKV)).one().value == "solar"
    assert session.scalars(scoped(bob, SettingKV)).one().value == "dark"
    session.execute(scoped_delete(alice, SettingKV))
    assert session.scalars(scoped(alice, SettingKV)).all() == []
    assert session.scalars(scoped(bob, SettingKV)).one().value == "dark"


def test_scoped_statements_carry_the_marker(users: tuple[User, User]) -> None:
    alice, _ = users
    for statement in (
        scoped(alice, SettingKV),
        scoped_update(alice, SettingKV),
        scoped_delete(alice, SettingKV),
    ):
        assert statement.get_execution_options() == {SCOPE_OPTION: alice.id}


def test_scoped_statement_composes_further(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    statement = scoped(alice, SettingKV).where(SettingKV.key == "theme").order_by(SettingKV.id)
    assert session.scalars(statement).one().value == "light"
    assert session.scalars(scoped(alice, SettingKV).where(SettingKV.key == "nope")).all() == []


def test_get_scoped_returns_the_row_or_none(session: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    alices = session.scalars(scoped(alice, SettingKV)).one()
    assert get_scoped(session, alice, SettingKV, alices.id) is alices
    assert get_scoped(session, bob, SettingKV, alices.id) is None
    assert get_scoped(session, alice, SettingKV, 10_000) is None


def test_get_scoped_never_loads_another_users_row(
    session: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    bobs_id = session.scalars(scoped(bob, SettingKV)).one().id
    session.expunge_all()
    assert get_scoped(session, alice, SettingKV, bobs_id) is None
    assert list(session) == []  # the filter is in the SQL, so nothing entered the identity map


# --- the escape hatch and what the guard ignores ----------------------------


def test_unscoped_marks_the_statement_and_logs_at_debug(
    session: Session, users: tuple[User, User], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="netkeeper.scoping"):
        statement = unscoped(select(SettingKV))
    assert statement.get_execution_options() == {UNSCOPED_OPTION: True}
    assert "unscoped statement on SettingKV" in caplog.text
    assert len(session.scalars(statement).all()) == 2
    session.execute(unscoped(update(SettingKV).values(value=0)))
    assert [row.value for row in session.scalars(statement)] == [0, 0]
    session.execute(unscoped(delete(SettingKV)))
    assert session.scalars(statement).all() == []


def test_statements_on_user_are_unaffected(session: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    assert session.scalar(select(func.count()).select_from(User)) == 2
    session.execute(update(User).where(User.id == alice.id).values(display_name="A"))
    session.execute(delete(User).where(User.id == bob.id))
    session.expunge_all()
    assert [u.display_name for u in session.scalars(select(User))] == ["A"]


def test_refresh_and_lazy_loads_are_exempt(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    row = session.scalars(scoped(alice, SettingKV)).one()
    session.refresh(row)
    session.expire(row)
    assert row.key == "theme"  # an expired attribute loads by primary key
    assert row.user is alice  # a relationship lazy load


def test_text_statements_are_not_inspected(session: Session, users: tuple[User, User]) -> None:
    # Documented hole: nothing to walk. Keep text() out of services.
    assert session.scalar(text("SELECT count(*) FROM settings_kv")) == 2


def test_owned_models_in_finds_tables_anywhere() -> None:
    assert owned_models_in(select(User)) == []
    assert owned_models_in(select(User).join(SettingKV)) == [SettingKV]
    assert owned_models_in(select(select(SettingKV.id).subquery())) == [SettingKV]
    assert owned_models_in(text("SELECT 1")) == []


# --- flush check ------------------------------------------------------------


def test_flush_rejects_a_new_owned_row_without_a_user(
    session: Session, users: tuple[User, User]
) -> None:
    session.add(SettingKV(key="k", value=1))
    with pytest.raises(UnscopedQueryError, match="new SettingKV has no user_id"):
        session.flush()


def test_flush_rejects_an_explicit_none_user_id(session: Session) -> None:
    session.add(SettingKV(user_id=None, key="k", value=1))
    with pytest.raises(UnscopedQueryError, match="new SettingKV has no user_id"):
        session.flush()


def test_flush_accepts_a_user_set_through_the_relationship(
    session: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    session.add(SettingKV(user=alice, key="k", value=1))
    session.flush()
    assert get_setting(session, alice, "k") == 1


# --- installation -----------------------------------------------------------


def test_guard_is_scoped_to_its_factory(
    engine: Engine, session: Session, users: tuple[User, User]
) -> None:
    session.commit()
    with make_session_factory(engine)() as unguarded:
        assert len(unguarded.scalars(select(SettingKV)).all()) == 2
    with Session(engine) as plain:
        assert len(plain.scalars(select(SettingKV)).all()) == 2


def test_raise_on_violation_false_logs_and_installing_twice_is_a_no_op(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    factory = make_session_factory(engine)
    install_scope_guard(factory, raise_on_violation=False)
    install_scope_guard(factory)  # no-op: the first install stands
    with factory() as session, caplog.at_level(logging.ERROR, logger="netkeeper.scoping"):
        assert session.scalars(select(SettingKV)).all() == []
        session.add(SettingKV(key="k", value=1))
        with pytest.raises(Exception, match="NOT NULL"):
            session.flush()  # logged, not raised, so the constraint speaks instead
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    assert "unscoped select touching SettingKV" in errors[0]
    assert "new SettingKV has no user_id" in errors[1]


async def test_the_app_session_factory_is_guarded(running_app: FastAPI) -> None:
    with running_app.state.session_factory() as session:
        assert session.scalars(select(User)).one().kind is UserKind.LOCAL
        with pytest.raises(UnscopedQueryError):
            session.execute(select(SettingKV))


# --- settings_kv ------------------------------------------------------------


def test_set_setting_creates_then_updates(session: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    row = set_setting(session, alice, "pace", {"per_day": 20})
    assert row.id is not None
    assert row.user_id == alice.id
    again = set_setting(session, alice, "pace", {"per_day": 30})
    assert again is row
    assert get_setting(session, alice, "pace") == {"per_day": 30}
    assert session.scalar(unscoped(select(func.count()).select_from(SettingKV))) == 3
    assert get_setting(session, bob, "pace") is None
    assert get_setting(session, bob, "pace", default=0) == 0


def test_settings_are_per_user(session: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    assert get_setting(session, alice, "theme") == "light"
    assert get_setting(session, bob, "theme") == "dark"
    set_setting(session, alice, "theme", "solar")
    assert get_setting(session, alice, "theme") == "solar"
    assert get_setting(session, bob, "theme") == "dark"


def test_delete_setting(session: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    assert delete_setting(session, alice, "theme") is True
    assert delete_setting(session, alice, "theme") is False
    assert get_setting(session, alice, "theme") is None
    assert get_setting(session, bob, "theme") == "dark"
