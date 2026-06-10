"""Orchestration: run one/looping check cycles, detect hits, notify, persist state.

Run:
  python -m src.monitor --once          # one cycle (für cron / systemd-timer)
  python -m src.monitor --loop          # Dauerbetrieb mit internem Intervall
  python -m src.monitor --test-notify   # Test-Push über ntfy senden
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import time
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler

from .config import Settings, load_settings
from .notify import ntfy_send
from .scraper import (
    STATUS_CAPTCHA,
    STATUS_NETWORK,
    STATUS_OK,
    STATUS_STRUCTURE,
    CycleResult,
    DayInfo,
    check_once,
)

log = logging.getLogger("fs_monitor")

# Re-send a reminder if the same in-window hit is still there after this long.
REMIND_AFTER_SECONDS = 3 * 3600


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def dates_in_window(dates: list[date], settings: Settings) -> list[date]:
    return [d for d in dates if settings.target_start <= d <= settings.target_end]


def format_hit_lines(hit_days: list[DayInfo]) -> list[str]:
    lines = []
    for di in sorted(hit_days, key=lambda x: x.day):
        if di.times:
            shown = ", ".join(di.times[:4])
            more = "…" if len(di.times) > 4 else ""
            tail = f" – {shown}{more}"
        else:
            tail = ""
        lines.append(f"• {di.label}{tail}")
    return lines


def build_hit_body(settings: Settings, hit_days: list[DayInfo]) -> str:
    return (
        "Früherer Termin in der Führerscheinstelle Oldenburg verfügbar!\n\n"
        + "\n".join(format_hit_lines(hit_days))
        + f"\n\nDein aktueller Termin: {settings.current_appointment.strftime('%d.%m.%Y')}"
        + f"\nZielfenster: {settings.target_start.strftime('%d.%m.%Y')}"
        + f"–{settings.target_end.strftime('%d.%m.%Y')}"
        + f"\n\nJetzt selbst buchen: {settings.booking_url}"
    )


# --------------------------------------------------------------------------- #
# Logging & state
# --------------------------------------------------------------------------- #
def setup_logging(settings: Settings) -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    file_handler = RotatingFileHandler(
        settings.log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)


def load_state(settings: Settings) -> dict:
    try:
        return json.loads(settings.state_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(settings: Settings, state: dict) -> None:
    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    settings.state_file.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Cycle handling
# --------------------------------------------------------------------------- #
def _handle_hits(settings: Settings, state: dict, res: CycleResult, now: datetime) -> None:
    hit_days = [d for d in res.days if settings.target_start <= d.day <= settings.target_end]
    current = {d.day.isoformat() for d in hit_days}
    previous = set(state.get("alerted_dates", []))

    if not current:
        if previous:
            log.info("Zielfenster-Termine wieder verschwunden.")
        state["alerted_dates"] = []
        state.pop("alerted_at", None)
        return

    new = current - previous
    remind = False
    if not new and state.get("alerted_at"):
        try:
            elapsed = (now - datetime.fromisoformat(state["alerted_at"])).total_seconds()
            remind = elapsed > REMIND_AFTER_SECONDS
        except Exception:  # noqa: BLE001
            remind = False

    if new or remind:
        title = "Frueher Termin verfuegbar!" if new else "Erinnerung: frueher Termin"
        ntfy_send(
            settings,
            title=title,
            message=build_hit_body(settings, hit_days),
            priority="urgent" if new else "high",
            tags=["tada", "calendar"],
            click=settings.booking_url,
            actions=f"view, Jetzt Termin buchen, {settings.booking_url}, clear=true",
        )
        state["alerted_dates"] = sorted(current)
        state["alerted_at"] = now.isoformat()
        log.info("ALERT gesendet für Zielfenster-Tage: %s", sorted(current))
    else:
        log.info("Treffer unverändert (bereits alarmiert): %s", sorted(current))


def _maybe_validation_ping(settings: Settings, state: dict, res: CycleResult, now: datetime) -> None:
    """Einmaliger Funktionstest: beim ERSTEN Zyklus, der irgendeinen freien Termin
    sieht (Datum egal), genau eine Bestätigungs-Push senden und sich dann selbst
    deaktivieren (im State gemerkt)."""
    if not settings.validation_ping or state.get("validation_fired"):
        return
    if not res.available_dates:
        return
    earliest = res.earliest
    body = (
        "✅ Funktionstest bestanden!\n\n"
        "Der Bot hat gerade in der GitHub-Cloud einen freien Termin erkannt und dir diese "
        "Push geschickt — egal ob dein Laptop an, aus oder zugeklappt ist.\n\n"
        f"Gesichteter Termin: {earliest.strftime('%d.%m.%Y') if earliest else '—'} "
        "(Datum egal, reiner Beweis).\n\n"
        "Ab jetzt kommt nur noch der ECHTE Alarm, sobald ein Termin im Fenster "
        f"{settings.target_start.strftime('%d.%m.%Y')}–{settings.target_end.strftime('%d.%m.%Y')} frei wird."
    )
    ntfy_send(
        settings,
        title="FS-Monitor Funktionstest bestanden",
        message=body,
        priority="high",
        tags=["white_check_mark", "satellite"],
    )
    state["validation_fired"] = True
    log.info("VALIDATION-Ping gesendet (gesichtet: %s) — einmalig, jetzt deaktiviert.",
             [d.isoformat() for d in res.available_dates])


def _handle_error_once(
    settings: Settings,
    state: dict,
    kind: str,
    now: datetime,
    *,
    title: str,
    priority: str,
    body: str,
    tags: list[str],
) -> None:
    if state.get("last_error_kind") != kind:
        ntfy_send(settings, title=title, message=body, priority=priority, tags=tags, click=settings.base_url)
        log.warning("Fehler-Alert (%s) gesendet.", kind)
    else:
        log.warning("Fehler (%s) besteht weiter – kein erneuter Alert.", kind)
    state["last_error_kind"] = kind
    state["last_error_at"] = now.isoformat()


def run_cycle(settings: Settings, state: dict) -> CycleResult:
    res = check_once(settings)
    now = datetime.now(timezone.utc)

    if res.status == STATUS_OK:
        if state.get("last_error_kind"):
            log.info("Ablauf wieder normal (vorheriger Fehler behoben).")
            for key in ("last_error_kind", "last_error_at", "last_error_msg"):
                state.pop(key, None)
        offered = [d.isoformat() for d in res.available_dates]
        window = [d.isoformat() for d in dates_in_window(res.available_dates, settings)]
        log.info(
            "OK | angeboten: %s | im Zielfenster: %s | frühester: %s",
            offered or "—",
            window or "—",
            res.earliest.isoformat() if res.earliest else "—",
        )
        _handle_hits(settings, state, res, now)
        _maybe_validation_ping(settings, state, res, now)

    elif res.status == STATUS_CAPTCHA:
        state["last_error_msg"] = res.message
        _handle_error_once(
            settings, state, STATUS_CAPTCHA, now,
            title="FS-Monitor: Captcha",
            priority="high",
            body=(
                "Auf der Buchungsseite erscheint eine Sicherheitsabfrage/Captcha.\n"
                "Der Bot umgeht das bewusst NICHT – bitte manuell prüfen:\n"
                f"{settings.base_url}"
            ),
            tags=["warning", "robot"],
        )

    elif res.status == STATUS_STRUCTURE:
        state["last_error_msg"] = res.message
        _handle_error_once(
            settings, state, STATUS_STRUCTURE, now,
            title="FS-Monitor: Seitenstruktur geaendert",
            priority="high",
            body=(
                "Der Buchungs-Ablauf konnte nicht durchlaufen werden – die Seite hat "
                "sich vermutlich geändert.\n"
                f"Detail: {res.message}\n\nBitte manuell prüfen: {settings.base_url}"
            ),
            tags=["warning", "wrench"],
        )

    else:  # STATUS_NETWORK
        state["last_error_msg"] = res.message
        _handle_error_once(
            settings, state, STATUS_NETWORK, now,
            title="FS-Monitor: Seite nicht erreichbar",
            priority="default",
            body=(
                "Die Buchungsseite ist aktuell nicht erreichbar.\n"
                f"Detail: {res.message}\n\nDer Bot versucht es weiter."
            ),
            tags=["warning", "globe_with_meridians"],
        )

    state["last_run_at"] = now.isoformat()
    state["last_status"] = res.status
    return res


# --------------------------------------------------------------------------- #
# Loop & CLI
# --------------------------------------------------------------------------- #
def run_loop(settings: Settings, state: dict) -> None:
    stop = {"flag": False}

    def _signal(_signum, _frame):
        stop["flag"] = True
        log.info("Stop-Signal erhalten – beende nach aktuellem Zyklus.")

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)

    log.info(
        "Loop-Start: alle %d min (±%ds Jitter) | Zielfenster %s–%s | aktueller Termin %s",
        settings.poll_interval_minutes,
        settings.poll_jitter_seconds,
        settings.target_start,
        settings.target_end,
        settings.current_appointment,
    )
    while not stop["flag"]:
        try:
            run_cycle(settings, state)
        except Exception as ex:  # noqa: BLE001 - loop must survive any single failure
            log.exception("Unerwarteter Fehler im Zyklus: %s", ex)
        finally:
            save_state(settings, state)

        if stop["flag"]:
            break
        jitter = random.randint(-settings.poll_jitter_seconds, settings.poll_jitter_seconds)
        wait = max(60, settings.poll_interval_seconds + jitter)
        slept = 0
        while slept < wait and not stop["flag"]:
            chunk = min(5, wait - slept)
            time.sleep(chunk)
            slept += chunk
    log.info("Monitor beendet.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fs-monitor",
        description="Termin-Monitor Führerscheinstelle Oldenburg (nur Benachrichtigung, keine Buchung).",
    )
    parser.add_argument("--once", action="store_true", help="Genau einen Prüf-Zyklus (für cron/systemd-timer).")
    parser.add_argument("--loop", action="store_true", help="Dauerbetrieb mit internem Intervall (systemd-service).")
    parser.add_argument("--test-notify", action="store_true", help="Test-Push über ntfy senden und beenden.")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings)

    if args.test_notify:
        ok = ntfy_send(
            settings,
            title="FS-Monitor Test",
            message="Test-Benachrichtigung – ntfy funktioniert. ✅",
            tags=["white_check_mark"],
        )
        return 0 if ok else 1

    state = load_state(settings)
    if args.loop:
        run_loop(settings, state)
    else:
        run_cycle(settings, state)
        save_state(settings, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
