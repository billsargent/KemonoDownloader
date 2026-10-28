"""Tests for configurable request pacing and retry backoff."""

from types import SimpleNamespace

import pytest
import requests

from kemonodownloader import request_pacing as rp


class FakeClock:
    """Deterministic replacement for ``request_pacing.time``."""

    def __init__(self, start=1000.0):
        self.now = start
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def reset_pacer():
    rp.pacer.reset()
    yield
    rp.pacer.reset()


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(rp, "time", fake)
    return fake


# ----------------------------------------------------------------------
# Parsing and formatting
# ----------------------------------------------------------------------


def test_parse_host_delays_valid():
    delays, errors = rp.parse_host_delays(
        "pawchive.pw=1.0\n# comment\n\nexample.com=0.25\n*.cdn.test=2\n"
    )
    assert errors == []
    assert delays == {
        "pawchive.pw": 1.0,
        "example.com": 0.25,
        "cdn.test": 2.0,
    }


def test_parse_host_delays_reports_invalid_lines():
    delays, errors = rp.parse_host_delays("good.host=1\nnonsense\nhost=abc\n=-1\n")
    assert delays == {"good.host": 1.0}
    assert errors == ["nonsense", "host=abc", "=-1"]


def test_parse_host_delays_empty_input():
    assert rp.parse_host_delays("") == ({}, [])
    assert rp.parse_host_delays(None) == ({}, [])


def test_format_host_delays_round_trip():
    delays = {"pawchive.pw": 1.0, "example.com": 0.5}
    text = rp.format_host_delays(delays)
    assert text == "pawchive.pw=1.0\nexample.com=0.5"
    assert rp.parse_host_delays(text)[0] == delays


# ----------------------------------------------------------------------
# Delay calculation
# ----------------------------------------------------------------------


def test_default_delays():
    assert rp.pacer.delay_for("https://kemono.cr/patreon/user/1") == 0.0
    assert rp.pacer.delay_for("https://file.pawchive.pw/data/x.png") == 1.0


def test_global_delay_applies_to_every_host():
    rp.pacer.configure(global_delay=0.75)
    assert rp.pacer.delay_for("https://kemono.cr/x") == 0.75
    assert rp.pacer.delay_for("https://coomer.st/y") == 0.75


def test_host_override_and_longest_match():
    rp.pacer.configure(
        global_delay=0.1,
        host_delays={"example.com": 0.5, "cdn.example.com": 2.0},
    )
    assert rp.pacer.delay_for("https://example.com/a") == 0.5
    assert rp.pacer.delay_for("https://cdn.example.com/a") == 2.0
    assert rp.pacer.delay_for("https://other.com/a") == 0.1


def test_global_delay_is_a_floor():
    rp.pacer.configure(global_delay=3.0, host_delays={"example.com": 1.0})
    assert rp.pacer.delay_for("https://example.com/a") == 3.0


def test_configure_clears_pending_schedule(clock):
    rp.pacer.configure(host_delays={"example.com": 5.0})
    assert rp.pacer.wait("https://example.com/a") is True

    rp.pacer.configure(host_delays={"example.com": 5.0})
    clock.now += 0.01
    assert rp.pacer.wait("https://example.com/a") is True
    assert clock.slept == []


# ----------------------------------------------------------------------
# Waiting
# ----------------------------------------------------------------------


def test_wait_returns_immediately_without_delay(clock):
    assert rp.pacer.wait("https://kemono.cr/a") is True
    assert clock.slept == []


def test_wait_enforces_spacing(clock):
    rp.pacer.configure(global_delay=2.0)

    assert rp.pacer.wait("https://kemono.cr/a") is True
    assert rp.pacer.wait("https://kemono.cr/b") is True

    assert sum(clock.slept) >= 2.0


def test_wait_returns_false_when_cancelled(clock):
    rp.pacer.configure(global_delay=5.0)
    rp.pacer.wait("https://kemono.cr/a")

    running = {"value": False}
    assert rp.pacer.wait("https://kemono.cr/b", lambda: running["value"]) is False
    assert clock.slept == []


def test_hosts_are_paced_independently(clock):
    rp.pacer.configure(global_delay=0.0, host_delays={"slow.test": 10.0})

    assert rp.pacer.wait("https://slow.test/a") is True
    assert rp.pacer.wait("https://fast.test/a") is True
    assert clock.slept == []


# ----------------------------------------------------------------------
# Backoff
# ----------------------------------------------------------------------


def test_compute_backoff_doubles_and_caps():
    rp.pacer.configure(backoff_base=1.0, backoff_cap=8.0)
    assert [rp.compute_backoff(i) for i in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_compute_backoff_without_cap():
    rp.pacer.configure(backoff_base=0.5, backoff_cap=0.0)
    assert rp.compute_backoff(3) == 4.0


def test_compute_backoff_disabled():
    rp.pacer.configure(backoff_base=0.0)
    assert rp.compute_backoff(5) == 0.0


def test_negative_attempt_is_treated_as_zero():
    rp.pacer.configure(backoff_base=2.0, backoff_cap=30.0)
    assert rp.compute_backoff(-3) == 2.0


# ----------------------------------------------------------------------
# paced_get / PacedSession
# ----------------------------------------------------------------------


class _FakeSession:
    def __init__(self):
        self.calls = []
        self.timeout = "session-timeout"

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return "response"


def test_paced_get_passes_through(clock):
    session = _FakeSession()
    result = rp.paced_get(session, "https://kemono.cr/a", headers={"X": "1"}, timeout=5)
    assert result == "response"
    assert session.calls == [
        ("https://kemono.cr/a", {"headers": {"X": "1"}, "timeout": 5})
    ]
    assert clock.slept == []


def test_paced_get_raises_when_cancelled(clock):
    rp.pacer.configure(global_delay=10.0)
    rp.pacer.wait("https://kemono.cr/first")

    session = _FakeSession()
    with pytest.raises(rp.RequestCancelled):
        rp.paced_get(session, "https://kemono.cr/second", is_running=lambda: False)
    assert session.calls == []
    assert isinstance(rp.RequestCancelled("x"), requests.RequestException)


def test_paced_session_wraps_get_and_proxies_attributes(clock):
    raw = _FakeSession()
    session = rp.PacedSession(raw)

    session.get("https://kemono.cr/a", timeout=1)

    assert raw.calls == [("https://kemono.cr/a", {"timeout": 1})]
    assert session.timeout == "session-timeout"


# ----------------------------------------------------------------------
# Settings integration
# ----------------------------------------------------------------------


def test_settings_apply_configures_pacer(tmp_path):
    from kemonodownloader.kd_settings import SettingsTab

    tab = SettingsTab.__new__(SettingsTab)
    SettingsTab._apply_pacing_settings(
        tab,
        {
            "request_delay_seconds": 1.5,
            "request_host_delays": "slow.test=4.0\nbad-line",
            "retry_backoff_seconds": 2.5,
            "retry_backoff_max_seconds": 12.0,
        },
    )

    assert rp.pacer.global_delay == 1.5
    assert rp.pacer.host_delays() == {"slow.test": 4.0}
    assert rp.pacer.backoff_base == 2.5
    assert rp.pacer.backoff_cap == 12.0
    assert rp.pacer.delay_for("https://slow.test/x") == 4.0


def test_settings_defaults_include_pacing(tmp_path, monkeypatch):
    from PyQt6.QtCore import QSettings

    import kemonodownloader.kd_settings as ks

    # Use an isolated settings store so the assertion is independent of the
    # user's real configuration.
    ini_path = str(tmp_path / "pacing_settings.ini")
    monkeypatch.setattr(
        ks,
        "QSettings",
        lambda *args, **kwargs: QSettings(ini_path, QSettings.Format.IniFormat),
    )

    parent = SimpleNamespace(
        base_folder="",
        download_folder="",
        cache_folder="",
        other_files_folder="",
        ensure_folders_exist=lambda: None,
        post_tab=SimpleNamespace(),
        creator_tab=SimpleNamespace(),
    )
    tab = ks.SettingsTab(parent)

    assert tab.get_request_delay_seconds() == 0.0
    assert tab.get_request_host_delays() == "pawchive.pw=1.0"
    assert tab.get_retry_backoff_seconds() == 1.0
    assert tab.get_retry_backoff_max_seconds() == 30.0
    assert rp.pacer.delay_for("https://pawchive.pw/data/x") == 1.0
