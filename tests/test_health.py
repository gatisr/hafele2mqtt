"""The container healthcheck, and the log sanitizing that feeds the same files.

Both are about what a person reads afterwards: one says whether the process is
still working, the other says what actually arrived. Neither is worth much if it
can be made to say something else.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from app import health
from app.config import ConfigError, Settings
from app.main import sanitize

ROOT = Path(__file__).resolve().parents[1]


# --- the staleness limit ------------------------------------------------------

def test_the_limit_is_three_poll_intervals():
    assert health.limit_for(300.0) == 900.0


def test_a_short_poll_interval_does_not_make_a_hair_trigger():
    """Three 30s intervals is a window one slow round trips.

    The healthcheck is there to tell a stopped bridge from a running one, not
    to measure one, so it has a floor under it.
    """
    assert health.limit_for(30.0) == health.FLOOR


def test_polling_turned_off_still_leaves_a_usable_limit():
    """POLL_INTERVAL=0 would otherwise be a limit of zero, i.e. always stale."""
    assert health.limit_for(0.0) == health.FLOOR


def test_an_explicit_limit_wins_over_the_derived_one():
    assert health.limit_for(300.0, 60.0) == 60.0


def test_the_floor_does_not_override_a_deliberate_choice():
    """Someone who sets 10 means 10, even though the default would never be."""
    assert health.limit_for(300.0, 10.0) == 10.0


# --- reading the heartbeat ----------------------------------------------------

def test_a_fresh_heartbeat_is_healthy(tmp_path):
    p = tmp_path / "health"
    p.touch()
    assert health.check(str(p), 180.0) == ""


def test_a_stale_heartbeat_says_how_stale_and_what_it_means(tmp_path):
    p = tmp_path / "health"
    p.touch()
    old = time.time() - 600
    os.utime(p, (old, old))
    problem = health.check(str(p), 180.0)
    assert "600s ago" in problem
    assert "180s" in problem
    # The distinction the whole design rests on: the process is up, the loop is
    # not going round. A reader acting on this must not go looking at the radio.
    assert "poll loop has stopped" in problem


def test_a_missing_heartbeat_names_start_up_as_the_innocent_explanation(tmp_path):
    problem = health.check(str(tmp_path / "health"), 180.0)
    assert "does not exist yet" in problem
    assert "start-up" in problem


def test_an_empty_path_turns_the_healthcheck_off(tmp_path):
    """HEALTH_FILE="" is a documented way to opt out, so it cannot report a fault."""
    assert health.check("", 180.0) == ""


def test_the_age_is_measured_against_the_clock_passed_in(tmp_path):
    p = tmp_path / "health"
    p.touch()
    assert health.check(str(p), 180.0, now=os.stat(p).st_mtime + 179) == ""
    assert health.check(str(p), 180.0, now=os.stat(p).st_mtime + 181) != ""


# --- the entry point ----------------------------------------------------------

def test_main_is_healthy_on_a_fresh_heartbeat(tmp_path, monkeypatch):
    p = tmp_path / "health"
    p.touch()
    monkeypatch.setenv("HEALTH_FILE", str(p))
    monkeypatch.setenv("MESH_JSON", "{}")
    assert health.main() == 0


def test_main_is_unhealthy_on_a_stale_one(tmp_path, monkeypatch, capsys):
    p = tmp_path / "health"
    p.touch()
    old = time.time() - 10_000
    os.utime(p, (old, old))
    monkeypatch.setenv("HEALTH_FILE", str(p))
    monkeypatch.setenv("MESH_JSON", "{}")
    assert health.main() == 1
    assert "poll loop" in capsys.readouterr().err


def test_a_container_holding_a_refused_setting_is_not_healthy(tmp_path, monkeypatch, capsys):
    """The bridge exits on this, so the heartbeat it left behind means nothing."""
    p = tmp_path / "health"
    p.touch()
    monkeypatch.setenv("HEALTH_FILE", str(p))
    monkeypatch.setenv("HEALTH_MAX_AGE", "-1")
    assert health.main() == 1
    assert "HEALTH_MAX_AGE" in capsys.readouterr().err


def test_the_healthcheck_reads_the_settings_the_bridge_was_given(monkeypatch):
    monkeypatch.setenv("HEALTH_FILE", "/somewhere/else")
    monkeypatch.setenv("HEALTH_MAX_AGE", "42")
    s = Settings.from_env()
    assert s.health_file == "/somewhere/else"
    assert s.health_max_age == 42.0


def test_a_negative_limit_is_refused_rather_than_treated_as_zero(monkeypatch):
    monkeypatch.setenv("HEALTH_MAX_AGE", "-5")
    with pytest.raises(ConfigError, match="HEALTH_MAX_AGE"):
        Settings.from_env()


def test_the_healthcheck_costs_nothing_the_bridge_costs():
    """`python -m app.health` runs once a minute, so it must stay a leaf.

    Importing app.main would drag in bleak and paho to read one timestamp.
    """
    src = (ROOT / "app" / "health.py").read_text()
    assert "from .config import" in src
    assert "main" not in re.findall(r"^from \.(\w+) import", src, re.M)


# --- log sanitizing -----------------------------------------------------------

def test_a_newline_cannot_forge_a_second_log_line():
    forged = 'ON"}\n2026-01-01 00:00:00 CRITICAL bridge: mesh keys leaked'
    out = sanitize(forged)
    assert "\n" not in out
    assert "\\n" in out


def test_an_escape_cannot_repaint_the_terminal_reading_the_log():
    out = sanitize("\x1b[2J\x1b[Hgone")
    assert "\x1b" not in out
    assert out.startswith("\\x1b[2J")


def test_a_carriage_return_cannot_overwrite_the_line_it_is_on():
    assert sanitize("harmless\rSOMETHING ELSE") == "harmless\\rSOMETHING ELSE"


def test_c1_goes_too_because_some_viewers_act_on_it():
    r"""\x9b is a one-byte CSI, i.e. the same thing as ESC [ to whatever reads it."""
    assert sanitize("a\x9bb") == "a\\x9bb"


def test_the_unicode_line_separators_go_too():
    """They are newlines to anything that follows the standard, and they are
    escaped as \\u rather than \\x so the escape says which character it was."""
    assert sanitize("a\u2028b\u2029c") == "a\\u2028b\\u2029c"


def test_an_enormous_payload_cannot_push_the_rest_of_the_log_out_of_the_file():
    out = sanitize("x" * 10_000)
    assert out == "x" * 200 + "..."


def test_truncation_happens_before_escaping_so_the_result_stays_bounded():
    """Escaping first would turn a megabyte of NULs into four megabytes."""
    out = sanitize("\x00" * 10_000)
    assert len(out) <= 6 * 200 + 3


def test_a_real_command_survives_unchanged():
    """Sanitizing is worthless if it also mangles what you were trying to read."""
    payload = '{"state":"ON","brightness":128}'
    assert sanitize(payload) == payload


def test_a_topic_is_sanitized_the_same_way():
    assert sanitize("hafele/light/\x1b[31mfake\x1b[0m/set").count("\\x1b") == 2


def test_a_non_string_says_what_type_it_was():
    """A JSON value can be a number, a list or null, and repr is the only form
    in which "128" and 128 are still different afterwards."""
    assert sanitize(128) == "128"
    assert sanitize("128") == "128"
    assert sanitize(None) == "None"
    assert sanitize([1, 2]) == "[1, 2]"


def test_a_hostile_string_inside_a_non_string_is_escaped_too():
    out = sanitize({"k": "a\nb"})
    assert "\n" not in out


# --- supply chain -------------------------------------------------------------

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*-?\s*(?:uses|  uses):\s*(\S+)", re.M)


def test_there_are_workflows_to_check():
    assert WORKFLOWS


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_not_a_tag(wf):
    """A tag is a mutable pointer in somebody else's repository.

    docker.yml can publish an image, so whoever can move a tag it trusts can
    publish one too. This test is what stops the next person pasting `@v4` back.
    """
    for ref in USES.findall(wf.read_text()):
        _, _, pin = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", pin), f"{ref} in {wf.name} is not a commit SHA"


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_every_pin_says_which_version_it_is(wf):
    """A bare SHA is unreadable, and Dependabot moves the pin and the comment
    together only if the comment is there to move."""
    for line in wf.read_text().splitlines():
        if re.match(r"^\s*-?\s*uses:", line):
            assert re.search(r"#\s*v?\d+\.\d+\.\d+", line), f"{line.strip()} has no version comment"


def test_dependabot_covers_both_things_that_are_pinned():
    """Pinning without Dependabot is just a pin that rots."""
    cfg = (ROOT / ".github" / "dependabot.yml").read_text()
    assert "package-ecosystem: github-actions" in cfg
    assert "package-ecosystem: pip" in cfg
