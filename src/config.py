"""Configuration loading & validation (from environment / .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Hard floor: never poll a public authority server more aggressively than this.
MIN_INTERVAL_MINUTES = 5


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


@dataclass(frozen=True)
class Settings:
    ntfy_server: str
    ntfy_topic: str
    ntfy_token: str | None
    target_start: date
    target_end: date
    current_appointment: date
    poll_interval_minutes: int
    poll_jitter_seconds: int
    headless: bool
    service_id: str
    base_url: str
    booking_url: str
    log_file: Path
    state_file: Path
    nav_timeout_ms: int
    validation_ping: bool = False

    @property
    def poll_interval_seconds(self) -> int:
        return self.poll_interval_minutes * 60


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else PROJECT_ROOT / p


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    """Load and validate settings from .env / environment. Raises SystemExit on
    fatal misconfiguration (missing topic, inverted date window)."""
    load_dotenv(env_file or PROJECT_ROOT / ".env")

    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic or "CHANGE-ME" in topic:
        raise SystemExit(
            "NTFY_TOPIC fehlt/ist Platzhalter in .env — bitte ein eigenes, schwer "
            "erratbares Topic setzen (siehe .env.example)."
        )

    interval = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))
    if interval < MIN_INTERVAL_MINUTES:
        print(
            f"[config] POLL_INTERVAL_MINUTES={interval} < {MIN_INTERVAL_MINUTES} — "
            f"auf {MIN_INTERVAL_MINUTES} angehoben (Behörden-Server schonen)."
        )
        interval = MIN_INTERVAL_MINUTES

    target_start = _parse_date(os.getenv("TARGET_START") or "2026-07-01")
    target_end = _parse_date(os.getenv("TARGET_END") or "2026-07-31")
    if target_start > target_end:
        raise SystemExit(f"TARGET_START ({target_start}) liegt nach TARGET_END ({target_end}).")

    current = os.getenv("CURRENT_APPOINTMENT", "").strip()
    current_appointment = _parse_date(current) if current else _parse_date("2026-07-31")

    base_url = os.getenv("BASE_URL", "https://terminvereinbarung.oldenburg.de/").rstrip("/") + "/"
    # Where the push/notification link points. Defaults to the start page; a deep
    # link (e.g. .../select2?md=2) lands the user one step further into the flow.
    booking_url = os.getenv("BOOKING_URL", "").strip() or base_url

    return Settings(
        ntfy_server=os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=topic,
        ntfy_token=(os.getenv("NTFY_TOKEN", "").strip() or None),
        target_start=target_start,
        target_end=target_end,
        current_appointment=current_appointment,
        poll_interval_minutes=interval,
        poll_jitter_seconds=int(os.getenv("POLL_JITTER_SECONDS", "45")),
        headless=os.getenv("HEADLESS", "true").lower() not in ("0", "false", "no"),
        service_id=os.getenv("SERVICE_ID", "301").strip(),
        base_url=base_url,
        booking_url=booking_url,
        log_file=_resolve(os.getenv("LOG_FILE", "logs/monitor.log")),
        state_file=_resolve(os.getenv("STATE_FILE", "state/state.json")),
        nav_timeout_ms=int(os.getenv("NAV_TIMEOUT_MS", "25000")),
        validation_ping=os.getenv("VALIDATION_PING", "").strip().lower() not in ("", "0", "false", "no"),
    )
