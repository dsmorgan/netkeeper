import sys
from pathlib import Path

import pytest

from netkeeper.paths import config_candidates, data_dir, ensure_data_dir


def test_data_dir_env_var_wins_over_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "custom"))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert data_dir() == tmp_path / "custom"


def test_data_dir_env_var_expands_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NETKEEPER_DATA", "~/nk")
    assert data_dir() == tmp_path / "nk"


def test_data_dir_empty_env_var_is_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NETKEEPER_DATA", "")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.chdir(tmp_path)
    assert data_dir() == tmp_path / "data"


def test_data_dir_macos_uses_application_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NETKEEPER_DATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert data_dir() == tmp_path / "Library" / "Application Support" / "netkeeper"


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_data_dir_other_platforms_use_cwd_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
) -> None:
    monkeypatch.delenv("NETKEEPER_DATA", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.chdir(tmp_path)
    assert data_dir() == tmp_path / "data"


def test_data_dir_does_not_create_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "fresh"))
    assert not data_dir().exists()
    assert not config_candidates(None)[-1].parent.exists()


def test_ensure_data_dir_creates_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "dir"
    monkeypatch.setenv("NETKEEPER_DATA", str(target))
    assert ensure_data_dir() == target
    assert target.is_dir()
    assert ensure_data_dir() == target  # already exists: no error


def test_config_candidates_full_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NETKEEPER_CONFIG", str(tmp_path / "env.toml"))
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "data"))
    explicit = tmp_path / "explicit.toml"
    assert config_candidates(explicit) == [
        explicit,
        tmp_path / "env.toml",
        cwd / "config.toml",
        tmp_path / "data" / "config.toml",
    ]


def test_config_candidates_without_explicit_or_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETKEEPER_CONFIG", "")
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "data"))
    assert config_candidates(None) == [
        tmp_path / "config.toml",
        tmp_path / "data" / "config.toml",
    ]


def test_config_candidates_env_var_expands_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NETKEEPER_CONFIG", "~/nk.toml")
    assert config_candidates(None)[0] == tmp_path / "nk.toml"


def test_config_candidates_explicit_expands_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_candidates(Path("~/nk.toml"))[0] == tmp_path / "nk.toml"
