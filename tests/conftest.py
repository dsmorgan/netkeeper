import pytest


@pytest.fixture(autouse=True)
def _clean_netkeeper_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's shell environment out of every test."""
    for name in ("NETKEEPER_DATA", "NETKEEPER_CONFIG", "NETKEEPER_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
