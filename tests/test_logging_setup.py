import logging
import re
from collections.abc import Iterator

import pytest

from netkeeper.logging_setup import HANDLER_NAME, RedactQueryFilter, setup_logging


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    yield
    root.setLevel(level)
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)


def _own_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if h.get_name() == HANDLER_NAME]


def test_defaults_to_info() -> None:
    setup_logging()
    assert logging.getLogger().level == logging.INFO


def test_env_var_level_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETKEEPER_LOG_LEVEL", "DEBUG")
    setup_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_env_var_is_case_insensitive_and_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETKEEPER_LOG_LEVEL", " warning ")
    setup_logging()
    assert logging.getLogger().level == logging.WARNING


def test_env_var_numeric_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETKEEPER_LOG_LEVEL", "10")
    setup_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_empty_env_var_means_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETKEEPER_LOG_LEVEL", "")
    setup_logging()
    assert logging.getLogger().level == logging.INFO


def test_argument_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETKEEPER_LOG_LEVEL", "DEBUG")
    setup_logging("ERROR")
    assert logging.getLogger().level == logging.ERROR


def test_int_argument() -> None:
    setup_logging(logging.WARNING)
    assert logging.getLogger().level == logging.WARNING


def test_bad_env_value_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("NETKEEPER_LOG_LEVEL", "LOUD")
    with caplog.at_level(logging.WARNING, logger="netkeeper.logging_setup"):
        setup_logging()
    assert logging.getLogger().level == logging.INFO
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "LOUD" in warnings[0].getMessage()
    assert "NETKEEPER_LOG_LEVEL" in warnings[0].getMessage()


def test_bad_argument_warns_and_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="netkeeper.logging_setup"):
        setup_logging("nonsense")
    assert logging.getLogger().level == logging.INFO
    assert any("nonsense" in r.getMessage() for r in caplog.records)


def test_negative_int_warns_and_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="netkeeper.logging_setup"):
        setup_logging(-5)
    assert logging.getLogger().level == logging.INFO
    assert any("-5" in r.getMessage() for r in caplog.records)


def test_idempotent_handler_installation() -> None:
    setup_logging("INFO")
    setup_logging("DEBUG")
    setup_logging()
    assert len(_own_handlers()) == 1
    assert logging.getLogger().level == logging.INFO  # last call wins


def test_one_line_per_record_with_timestamp_level_name_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO")
    logging.getLogger("netkeeper.example").info("hello %s", "world")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO netkeeper\.example hello world", lines[0]
    )


def test_records_below_level_are_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("WARNING")
    logging.getLogger("netkeeper.example").info("quiet")
    assert capsys.readouterr().err == ""


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "GET", path, "1.1", 303),
        None,
    )


def test_the_oauth_callbacks_query_never_reaches_the_access_log() -> None:
    """The query carries Google's authorization code (#244)."""
    record = _access_record("/api/v1/mailboxes/oauth/callback?state=s&code=4/secret")
    assert RedactQueryFilter().filter(record)
    assert "secret" not in record.getMessage()
    assert "/api/v1/mailboxes/oauth/callback?<redacted>" in record.getMessage()


def test_other_queries_are_logged_as_they_were() -> None:
    record = _access_record("/api/v1/contacts?q=x")
    assert RedactQueryFilter().filter(record)
    assert "/api/v1/contacts?q=x" in record.getMessage()


def test_setup_installs_the_redaction_on_the_access_log_once() -> None:
    setup_logging("INFO")
    setup_logging("INFO")
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(item, RedactQueryFilter) for item in access.filters) == 1
