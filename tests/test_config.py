import logging
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from netkeeper.cli import app
from netkeeper.config import (
    ConfigError,
    LegacyMe,
    LinkedInSettings,
    Settings,
    load_settings,
    render_toml,
)
from netkeeper.paths import data_dir

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty cwd and data dir so no real config file can leak into a test."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "data"))
    return tmp_path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- resolution order -------------------------------------------------------


def test_defaults_when_no_file_exists(isolated: Path) -> None:
    settings = load_settings()
    assert settings == Settings()
    assert settings.source_path is None


def _appendix_b() -> str:
    """The ```toml block under Appendix B in docs/architecture.md."""
    lines = (REPO_ROOT / "docs" / "architecture.md").read_text().splitlines(keepends=True)
    heading = next(i for i, line in enumerate(lines) if line.startswith("## Appendix B"))
    fence = next(i for i in range(heading, len(lines)) if lines[i].startswith("```toml"))
    end = next(i for i in range(fence + 1, len(lines)) if lines[i].startswith("```"))
    return "".join(lines[fence + 1 : end])


def test_example_config_is_appendix_b_verbatim() -> None:
    assert (REPO_ROOT / "config.example.toml").read_text() == _appendix_b()


def test_example_config_loads_cleanly_as_the_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    example = REPO_ROOT / "config.example.toml"
    with caplog.at_level(logging.WARNING, logger="netkeeper.config"):
        settings = load_settings(example)
    assert settings.source_path == example
    assert replace(settings, source_path=None) == Settings()
    assert settings.legacy_me is None
    assert caplog.records == []


def test_the_example_config_has_no_me_section() -> None:
    """#342: [me] is gone; your own details are the self contact."""
    assert "me" not in tomllib.loads((REPO_ROOT / "config.example.toml").read_text())
    assert not hasattr(Settings(), "me")


def test_explicit_path_wins(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = _write(isolated / "explicit.toml", "[web]\nport = 1111\n")
    _write(isolated / "env.toml", "[web]\nport = 2222\n")
    _write(isolated / "cwd" / "config.toml", "[web]\nport = 3333\n")
    _write(isolated / "data" / "config.toml", "[web]\nport = 4444\n")
    monkeypatch.setenv("NETKEEPER_CONFIG", str(isolated / "env.toml"))
    settings = load_settings(explicit)
    assert settings.web.port == 1111
    assert settings.source_path == explicit


def test_explicit_path_missing_is_an_error(isolated: Path) -> None:
    _write(isolated / "cwd" / "config.toml", "[web]\nport = 3333\n")
    missing = isolated / "nope.toml"
    with pytest.raises(ConfigError, match=str(missing)):
        load_settings(missing)


def test_env_var_used_when_no_explicit(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = _write(isolated / "env.toml", "[web]\nport = 2222\n")
    _write(isolated / "cwd" / "config.toml", "[web]\nport = 3333\n")
    _write(isolated / "data" / "config.toml", "[web]\nport = 4444\n")
    monkeypatch.setenv("NETKEEPER_CONFIG", str(env_file))
    settings = load_settings()
    assert settings.web.port == 2222
    assert settings.source_path == env_file


def test_env_var_pointing_at_missing_file_falls_through(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(isolated / "cwd" / "config.toml", "[web]\nport = 3333\n")
    monkeypatch.setenv("NETKEEPER_CONFIG", str(isolated / "missing.toml"))
    assert load_settings().web.port == 3333


def test_cwd_config_used_when_no_env(isolated: Path) -> None:
    cwd_file = _write(isolated / "cwd" / "config.toml", "[web]\nport = 3333\n")
    _write(isolated / "data" / "config.toml", "[web]\nport = 4444\n")
    settings = load_settings()
    assert settings.web.port == 3333
    assert settings.source_path == cwd_file


def test_data_dir_config_used_last(isolated: Path) -> None:
    data_file = _write(isolated / "data" / "config.toml", "[web]\nport = 4444\n")
    settings = load_settings()
    assert settings.web.port == 4444
    assert settings.source_path == data_file


# --- parsing and validation ---------------------------------------------------


def test_absent_keys_keep_defaults(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[linkedin.budget]\nwarmup_start = 5\n")
    settings = load_settings(path)
    assert settings.linkedin.budget.warmup_start == 5
    assert settings.linkedin.budget.warmup_step == 10
    assert settings.linkedin.cdp_url == LinkedInSettings().cdp_url
    assert settings.web == Settings().web


def test_lists_become_tuples(isolated: Path) -> None:
    path = _write(
        isolated / "c.toml",
        '[campaigns]\nholidays = ["2026-12-25", "2027-01-01"]\n',
    )
    settings = load_settings(path)
    assert settings.campaigns.holidays == ("2026-12-25", "2027-01-01")


@pytest.mark.parametrize(
    "body",
    [
        '[campaigns]\nsend_window_days = ["Tue", "Wed", "Thu"]\n'
        'send_window_hours = ["09:00", "16:30"]\nmailbox_daily_cap = 40\n',
        # Even a shape the old loader refused: the keys are not read at all.
        '[campaigns]\nsend_window_days = "Tue"\nsend_window_hours = [9]\nmailbox_daily_cap = 40\n',
    ],
)
def test_an_old_config_with_send_window_keys_loads_with_one_deprecation_warning(
    isolated: Path, caplog: pytest.LogCaptureFixture, body: str
) -> None:
    """#338: the hard send window is gone. A config that still has its keys loads, the keys
    are ignored, and one warning names both, rather than one unknown-key warning each."""
    path = _write(isolated / "c.toml", body)
    with caplog.at_level(logging.WARNING, logger="netkeeper.config"):
        settings = load_settings(path)
    assert settings.campaigns.mailbox_daily_cap == 40
    assert not hasattr(settings.campaigns, "send_window_days")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert (
        "deprecated keys campaigns.send_window_days, campaigns.send_window_hours" in (warnings[0])
    )
    assert "scheduled start" in warnings[0]


def test_one_deprecated_key_is_named_alone(
    isolated: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(isolated / "c.toml", '[campaigns]\nsend_window_hours = ["09:00", "16:30"]\n')
    with caplog.at_level(logging.WARNING, logger="netkeeper.config"):
        load_settings(path)
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "deprecated key campaigns.send_window_hours (" in warning


def test_unknown_key_warns_and_is_ignored(isolated: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = _write(isolated / "c.toml", '[linkedin]\nbogus = 1\ncdp_url = "http://x"\n')
    with caplog.at_level(logging.WARNING, logger="netkeeper.config"):
        settings = load_settings(path)
    assert settings.linkedin.cdp_url == "http://x"
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 1
    assert "linkedin.bogus" in messages[0]
    assert str(path) in messages[0]


def test_unknown_section_warns_and_is_ignored(
    isolated: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(isolated / "c.toml", "[mystery]\nx = 1\n\n[linkedin.nope]\ny = 2\n")
    with caplog.at_level(logging.WARNING, logger="netkeeper.config"):
        settings = load_settings(path)
    assert replace(settings, source_path=None) == Settings()
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("[mystery]" in m for m in messages)
    assert any("[linkedin.nope]" in m for m in messages)


def test_wrong_scalar_type_raises_naming_key_and_file(isolated: Path) -> None:
    path = _write(isolated / "c.toml", '[web]\nport = "8000"\n')
    with pytest.raises(ConfigError) as info:
        load_settings(path)
    assert "web.port" in str(info.value)
    assert str(path) in str(info.value)
    assert "integer" in str(info.value)


def test_bool_is_not_an_integer(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[web]\nport = true\n")
    with pytest.raises(ConfigError, match=r"web\.port"):
        load_settings(path)


def test_integer_is_not_a_bool(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[llm]\nenabled = 1\n")
    with pytest.raises(ConfigError, match=r"llm\.enabled"):
        load_settings(path)


def test_integer_accepted_for_float(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[linkedin.heat]\nper_block = 2\n")
    settings = load_settings(path)
    assert settings.linkedin.heat.per_block == 2.0
    assert isinstance(settings.linkedin.heat.per_block, float)


def test_float_rejected_for_integer(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[backup]\nkeep = 14.5\n")
    with pytest.raises(ConfigError, match=r"backup\.keep"):
        load_settings(path)


def test_nested_section_must_be_a_table(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[linkedin]\nbudget = 5\n")
    with pytest.raises(ConfigError, match=r"linkedin\.budget must be a table"):
        load_settings(path)


def test_top_level_section_must_be_a_table(isolated: Path) -> None:
    path = _write(isolated / "c.toml", 'web = "nope"\n')
    with pytest.raises(ConfigError, match="web must be a table"):
        load_settings(path)


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ('[linkedin]\nactive_hours = ["08:30"]\n', "linkedin.active_hours"),
        ('[linkedin]\nactive_hours = ["08:30", "12:00", "21:30"]\n', "linkedin.active_hours"),
        ('[linkedin]\nactive_hours = "08:30"\n', "linkedin.active_hours"),
        ('[linkedin]\nactive_hours = ["08:30", 2130]\n', "linkedin.active_hours[1]"),
        ("[linkedin.pacing]\nburst_size = [8]\n", "linkedin.pacing.burst_size"),
        ('[linkedin.pacing]\nburst_size = [8, "15"]\n', "linkedin.pacing.burst_size[1]"),
        ("[campaigns]\nholidays = [1]\n", "campaigns.holidays[0]"),
    ],
)
def test_list_shape_is_validated(isolated: Path, body: str, key: str) -> None:
    path = _write(isolated / "c.toml", body)
    with pytest.raises(ConfigError) as info:
        load_settings(path)
    assert key in str(info.value)


def test_an_old_me_section_loads_with_one_deprecation_warning_and_is_ignored(
    isolated: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#342: an older config still loads. ``[me]`` is left out of the settings, warned
    about once, and only name, website and city are kept aside to seed the self contact."""
    body = (
        '[me]\nname = " Ada Lovelace "\nwebsite = "https://ada.example"\nsignature = "A"\n'
        'scheduling_link = "https://ada.example/meet"\ncity = "London"\npodcast = "P"\n'
        "[web]\nport = 9001\n"
    )
    path = _write(isolated / "c.toml", body)
    with caplog.at_level(logging.WARNING, logger="netkeeper.config"):
        settings = load_settings(path)
    assert settings.web.port == 9001
    assert settings.legacy_me == LegacyMe(
        name="Ada Lovelace", website="https://ada.example", city="London"
    )
    assert replace(settings, source_path=None, legacy_me=None, web=Settings().web) == Settings()
    [warning] = [record.getMessage() for record in caplog.records]
    assert "ignoring the deprecated [me] section" in warning
    assert "About you" in warning and "Remove [me]" in warning


def test_an_old_me_section_with_values_that_are_not_strings_still_loads(
    isolated: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The section is ignored, so nothing in it can stop netkeeper from starting."""
    path = _write(isolated / "c.toml", '[me]\nname = 12\ncity = "Paris"\nyears = 3\n')
    with caplog.at_level(logging.WARNING, logger="netkeeper.config"):
        settings = load_settings(path)
    assert settings.legacy_me == LegacyMe(city="Paris")
    assert len(caplog.records) == 1


def test_a_config_without_me_has_no_legacy_values(isolated: Path) -> None:
    assert load_settings(_write(isolated / "c.toml", "[web]\nport = 1\n")).legacy_me is None


def test_malformed_toml_raises_config_error(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[web\nport = 1\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_settings(path)


def test_settings_are_frozen() -> None:
    settings = Settings()
    with pytest.raises(AttributeError):
        settings.web = replace(settings.web, port=1)  # type: ignore[misc]


# --- profile_visits_per_week: optional since #318 -----------------------------


def test_an_absent_weekly_profile_visit_limit_is_none(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[linkedin.budget]\nprofile_visits_per_day = 100\n")
    budget = load_settings(path).linkedin.budget
    assert budget.profile_visits_per_day == 100
    assert budget.profile_visits_per_week is None


def test_an_existing_config_that_sets_the_weekly_limit_still_loads_it(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[linkedin.budget]\nprofile_visits_per_week = 420\n")
    assert load_settings(path).linkedin.budget.profile_visits_per_week == 420


def test_a_weekly_limit_of_the_wrong_type_is_still_an_error(isolated: Path) -> None:
    path = _write(isolated / "c.toml", '[linkedin.budget]\nprofile_visits_per_week = "lots"\n')
    with pytest.raises(ConfigError, match="profile_visits_per_week must be an integer"):
        load_settings(path)


def test_render_toml_leaves_an_unset_weekly_limit_out_and_round_trips_a_set_one(
    isolated: Path,
) -> None:
    assert "profile_visits_per_week" not in render_toml(Settings())
    explicit = load_settings(
        _write(isolated / "c.toml", "[linkedin.budget]\nprofile_visits_per_week = 420\n")
    )
    rendered = render_toml(explicit)
    assert "profile_visits_per_week = 420" in rendered
    reloaded = load_settings(_write(isolated / "again.toml", rendered))
    assert reloaded.linkedin.budget.profile_visits_per_week == 420


# --- rendering and the CLI ----------------------------------------------------


def test_render_toml_round_trips_through_load_settings(isolated: Path) -> None:
    original = load_settings(
        _write(
            isolated / "c.toml",
            '[campaigns]\nholidays = ["2026-12-25"]\nlinkedin_auto_send = true\n'
            "[linkedin.pacing]\ndistraction_p = 0.25\n",
        )
    )
    reloaded = load_settings(_write(isolated / "again.toml", render_toml(original)))
    assert replace(reloaded, source_path=None) == replace(original, source_path=None)


def test_render_toml_of_defaults_matches_example_file() -> None:
    example = (REPO_ROOT / "config.example.toml").read_text()
    rendered = render_toml(Settings())
    assert tomllib.loads(rendered) == tomllib.loads(example)


def test_config_show_prints_settings_and_paths(isolated: Path) -> None:
    path = _write(isolated / "c.toml", "[web]\nport = 9999\n")
    result = CliRunner().invoke(app, ["--config", str(path), "config", "show"])
    assert result.exit_code == 0, result.output
    shown = tomllib.loads(result.stdout)
    assert shown["web"]["port"] == 9999
    assert shown["linkedin"]["budget"]["warmup_start"] == 20
    assert shown["paths"] == {"data_dir": str(data_dir()), "config": str(path)}
    assert result.stdout.rstrip().endswith(
        "[paths]\n" + f'data_dir = "{data_dir()}"\n' + f'config = "{path}"'
    )


def test_config_show_notes_the_derived_weekly_limit_when_it_is_unset(isolated: Path) -> None:
    """#319 review N2: an unset key has no TOML value, so the derived one is a comment."""
    unset = _write(isolated / "unset.toml", "[linkedin.budget]\nprofile_visits_per_day = 1000\n")
    result = CliRunner().invoke(app, ["--config", str(unset), "config", "show"])
    assert result.exit_code == 0, result.output
    assert (
        "profile_visits_per_day = 1000\n"
        "# profile_visits_per_week is unset: 1250 (5 x the daily limit in force)\n"
    ) in result.stdout
    assert "profile_visits_per_week" not in tomllib.loads(result.stdout)["linkedin"]["budget"]

    defaults = CliRunner().invoke(app, ["config", "show"])
    assert "# profile_visits_per_week is unset: 300 " in defaults.stdout

    explicit = _write(isolated / "set.toml", "[linkedin.budget]\nprofile_visits_per_week = 420\n")
    shown = CliRunner().invoke(app, ["--config", str(explicit), "config", "show"]).stdout
    assert "is unset" not in shown
    assert tomllib.loads(shown)["linkedin"]["budget"]["profile_visits_per_week"] == 420


def test_config_show_reports_defaults(isolated: Path) -> None:
    result = CliRunner().invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output
    shown = tomllib.loads(result.stdout)
    assert shown["paths"]["config"] == "defaults"
    assert shown["paths"]["data_dir"] == str(isolated / "data")
    assert shown["web"] == {"host": "127.0.0.1", "port": 8000}


def test_config_show_with_bad_config_exits_nonzero(isolated: Path) -> None:
    path = _write(isolated / "c.toml", '[web]\nport = "x"\n')
    result = CliRunner().invoke(app, ["--config", str(path), "config", "show"])
    assert result.exit_code == 1
    assert "web.port" in result.stderr
    assert "Traceback" not in result.output


def test_config_show_with_missing_explicit_file_exits_nonzero(isolated: Path) -> None:
    result = CliRunner().invoke(app, ["--config", str(isolated / "nope.toml"), "config", "show"])
    assert result.exit_code == 1
    assert "not found" in result.stderr
