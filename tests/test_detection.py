"""Pure-logic tests for date parsing and target-window detection.

Run standalone (no extra deps):  .venv/bin/python tests/test_detection.py
Or with pytest:                  .venv/bin/pytest tests/
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.monitor as monitor  # noqa: E402
from src.config import Settings  # noqa: E402
from src.monitor import build_hit_body, dates_in_window, format_hit_lines  # noqa: E402
from src.scraper import CycleResult, parse_days, parse_locations  # noqa: E402


class _Recorder:
    """Stand-in for ntfy_send that records calls instead of sending."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, settings, **kwargs):
        self.calls.append(kwargs)
        return True


def make_settings(**kw) -> Settings:
    base = dict(
        ntfy_server="https://ntfy.sh",
        ntfy_topic="topic",
        ntfy_token=None,
        target_start=date(2026, 6, 18),
        target_end=date(2026, 6, 25),
        current_appointment=date(2026, 6, 26),
        poll_interval_minutes=10,
        poll_jitter_seconds=45,
        headless=True,
        service_id="301",
        funktionseinheit="1",
        anliegen_category="KFZ-Angelegenheiten",
        base_url="https://terminvereinbarung.oldenburg.de/",
        booking_url="https://terminvereinbarung.oldenburg.de/select2?md=2",
        log_file=Path("logs/monitor.log"),
        state_file=Path("state/state.json"),
        nav_timeout_ms=25000,
    )
    base.update(kw)
    return Settings(**base)


def test_parse_days_extracts_dates_and_times():
    raw = [
        {"label": "Dienstag, 30.06.2026", "times": ["08:00", "08:15"]},
        {"label": "Mittwoch, 01.07.2026", "times": []},
        {"label": "kaputt ohne Datum", "times": ["09:00"]},
    ]
    days = parse_days(raw)
    assert [d.day for d in days] == [date(2026, 6, 30), date(2026, 7, 1)]
    assert days[0].times == ["08:00", "08:15"]


def test_parse_locations_reads_earliest_per_location():
    headers = [
        "1: Bürgerbüro Nord, Termine ab 12.06.2026, 08:30 Uhr",
        "2: Bürgerbüro Mitte, Termine ab 23.06.2026, 08:30 Uhr",
    ]
    locs = parse_locations(headers)
    assert [loc.day for loc in locs] == [date(2026, 6, 12), date(2026, 6, 23)]
    assert "Nord" in locs[0].label and "12.06.2026" in locs[0].label
    assert locs[0].times == ["08:30 Uhr"]  # Datum + Uhrzeit aus dem Standort-Kopf


def test_no_hit_when_only_later_dates():
    settings = make_settings()
    res = CycleResult(status="ok", days=parse_days([{"label": "Dienstag, 30.06.2026", "times": []}]))
    assert dates_in_window(res.available_dates, settings) == []


def test_hit_inside_window():
    settings = make_settings()
    res = CycleResult(
        status="ok",
        days=parse_days([
            {"label": "Montag, 22.06.2026", "times": ["09:00", "09:15", "09:30", "09:45", "10:00"]},
            {"label": "Dienstag, 30.06.2026", "times": []},
        ]),
    )
    hits = dates_in_window(res.available_dates, settings)
    assert hits == [date(2026, 6, 22)]

    hit_days = [d for d in res.days if d.day in hits]
    line = format_hit_lines(hit_days)[0]
    assert "22.06.2026" in line
    assert "09:00" in line
    assert line.endswith("…")  # more than 4 times -> ellipsis

    body = build_hit_body(settings, hit_days)
    assert "22.06.2026" in body
    assert "18.06.2026" in body  # Zielfenster-Start erwähnt


def test_boundary_dates_are_inclusive():
    settings = make_settings()
    res = CycleResult(
        status="ok",
        days=parse_days([
            {"label": "Donnerstag, 18.06.2026", "times": ["11:00"]},
            {"label": "Donnerstag, 25.06.2026", "times": ["12:00"]},
            {"label": "Freitag, 26.06.2026", "times": ["08:00"]},
        ]),
    )
    hits = dates_in_window(res.available_dates, settings)
    assert hits == [date(2026, 6, 18), date(2026, 6, 25)]


def test_hit_alert_dedup_and_reminder():
    settings = make_settings()
    rec = _Recorder()
    original = monitor.ntfy_send
    monitor.ntfy_send = rec
    try:
        state: dict = {}
        hit = CycleResult(status="ok", days=parse_days([{"label": "Montag, 22.06.2026", "times": ["09:00"]}]))
        t0 = datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc)

        monitor._handle_hits(settings, state, hit, t0)
        assert len(rec.calls) == 1                      # first detection -> alert
        assert state["alerted_dates"] == ["2026-06-22"]
        assert rec.calls[0]["priority"] == "urgent"

        monitor._handle_hits(settings, state, hit, t0 + timedelta(minutes=10))
        assert len(rec.calls) == 1                      # same hit soon after -> no re-alert

        monitor._handle_hits(settings, state, hit, t0 + timedelta(hours=4))
        assert len(rec.calls) == 2                      # past reminder window -> reminder
        assert rec.calls[1]["priority"] == "high"

        gone = CycleResult(status="ok", days=parse_days([{"label": "Dienstag, 30.06.2026", "times": []}]))
        monitor._handle_hits(settings, state, gone, t0 + timedelta(hours=5))
        assert len(rec.calls) == 2                      # hit gone -> no alert, state cleared
        assert state["alerted_dates"] == []
    finally:
        monitor.ntfy_send = original


def test_error_alert_once_per_kind():
    settings = make_settings()
    rec = _Recorder()
    original = monitor.ntfy_send
    monitor.ntfy_send = rec
    try:
        state: dict = {}
        now = datetime(2026, 6, 8, tzinfo=timezone.utc)
        kw = dict(title="t", priority="default", body="b", tags=["x"])
        monitor._handle_error_once(settings, state, "network_error", now, **kw)
        monitor._handle_error_once(settings, state, "network_error", now, **kw)
        assert len(rec.calls) == 1                      # same error kind -> alert once
        monitor._handle_error_once(settings, state, "captcha", now, title="t2", priority="high", body="b2", tags=["y"])
        assert len(rec.calls) == 2                      # new error kind -> new alert
    finally:
        monitor.ntfy_send = original


def test_validation_ping_fires_once():
    settings = make_settings(validation_ping=True)
    rec = _Recorder()
    original = monitor.ntfy_send
    monitor.ntfy_send = rec
    try:
        now = datetime(2026, 6, 10, tzinfo=timezone.utc)
        state: dict = {}
        res = CycleResult(status="ok", days=parse_days([{"label": "Donnerstag, 02.07.2026", "times": ["08:00"]}]))
        monitor._maybe_validation_ping(settings, state, res, now)
        assert len(rec.calls) == 1 and state.get("validation_fired") is True
        monitor._maybe_validation_ping(settings, state, res, now)   # darf nicht erneut feuern
        assert len(rec.calls) == 1
        monitor._maybe_validation_ping(settings, {}, CycleResult(status="ok", days=[]), now)  # ohne Termin: kein Ping
        assert len(rec.calls) == 1
    finally:
        monitor.ntfy_send = original


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as ex:
            failed += 1
            print(f"  FAIL {fn.__name__}: {ex}")
    print(f"\n{len(tests) - failed}/{len(tests)} Tests bestanden.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
