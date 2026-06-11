"""Playwright flow for terminvereinbarung.oldenburg.de (TEVIS).

Drives the booking wizard up to "Schritt 4 - Auswahl der Zeit" and reads the
offered days. It NEVER books anything and NEVER tries to solve a captcha — if a
captcha/security check appears it stops and reports it.

Validated click path (June 2026):
  URL
  -> #cookie_msg_btn_no                 (reject cookies, if banner present)
  -> #buttonfunktionseinheit-2          (Führerscheinstelle)
  -> #button-plus-<service_id>          (Anliegen "Führerschein / Fahrerlaubnis allgemein" = 301)
  -> #WeiterButton
  -> modal: click each label.labelChecklist, then #OKButton
  -> Schritt 3: #WeiterButton           (single, preselected location)
  -> Schritt 4: read h3.ui-accordion-header  (one per available day)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from .config import Settings

log = logging.getLogger("fs_monitor.scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]
CAPTCHA_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "g-recaptcha",
    "sicherheitsabfrage", "ich bin kein roboter", "bot-erkennung",
)
NO_SLOTS_MARKERS = (
    "keine freien termine", "keine termine", "derzeit keine", "kein termin",
    "keine verfügbaren", "ausgebucht",
)
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{2})\.(\d{4})\b")

# Read one entry per available day: the accordion header (date) + its slot times.
DAY_HEADER_JS = r"""() => {
  const out = [];
  document.querySelectorAll('h3.ui-accordion-header').forEach(h => {
    const label = (h.textContent || '').replace(/\s+/g, ' ').trim();
    const panel = h.nextElementSibling;
    let times = [];
    if (panel) {
      times = [...panel.querySelectorAll('.suggest_btn')]
        .map(a => (a.textContent || '').trim())
        .filter(t => /^\d{1,2}:\d{2}$/.test(t));
    }
    out.push({ label, times });
  });
  return out;
}"""

# Bürgerbüro: one accordion header per location, e.g.
# "1: Bürgerbüro Nord, Termine ab 12.06.2026, 08:30 Uhr".
LOCATION_HEADER_JS = r"""() => [...document.querySelectorAll('.ui-accordion-header')]
  .map(e => (e.textContent || '').replace(/\s+/g, ' ').trim())
  .filter(t => /Termine ab\s+\d{1,2}\.\d{2}\.\d{4}/i.test(t))"""

# OK status values:
STATUS_OK = "ok"
STATUS_CAPTCHA = "captcha"
STATUS_STRUCTURE = "structure_error"
STATUS_NETWORK = "network_error"


class CaptchaDetected(Exception):
    """Raised when a captcha / security check appears in the flow."""


class StructureError(Exception):
    """Raised when an expected element is missing (page layout likely changed)."""


@dataclass
class DayInfo:
    day: date
    label: str
    times: list[str] = field(default_factory=list)


@dataclass
class CycleResult:
    status: str
    days: list[DayInfo] = field(default_factory=list)
    message: str = ""

    @property
    def available_dates(self) -> list[date]:
        return sorted({d.day for d in self.days})

    @property
    def earliest(self) -> date | None:
        dates = self.available_dates
        return dates[0] if dates else None

    def times_for(self, day: date) -> list[str]:
        for d in self.days:
            if d.day == day:
                return d.times
        return []


def parse_days(raw: list[dict]) -> list[DayInfo]:
    """Turn the raw [{label, times}] from the page into typed, date-parsed days."""
    days: list[DayInfo] = []
    for item in raw:
        label = (item.get("label") or "").strip()
        match = DATE_RE.search(label)
        if not match:
            continue
        d, mo, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            parsed = date(y, mo, d)
        except ValueError:
            continue
        times = [t for t in (item.get("times") or []) if isinstance(t, str)]
        days.append(DayInfo(day=parsed, label=label, times=times))
    days.sort(key=lambda x: x.day)
    return days


LOCATION_DATE_RE = re.compile(r"Termine ab\s+(\d{1,2})\.(\d{2})\.(\d{4})", re.I)


def parse_locations(headers: list[str]) -> list[DayInfo]:
    """Bürgerbüro: each location header states its earliest free date. Turn them
    into DayInfos keyed by that date (label keeps the location name for alerts)."""
    days: list[DayInfo] = []
    for header in headers:
        match = LOCATION_DATE_RE.search(header)
        if not match:
            continue
        d, mo, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            earliest = date(y, mo, d)
        except ValueError:
            continue
        name = header[: match.start()].strip().rstrip(",").strip()
        name = re.sub(r"^\d+\s*[:.]\s*", "", name) or "Standort"
        days.append(DayInfo(
            day=earliest,
            label=f"{name} – frühester Termin {earliest.strftime('%d.%m.%Y')}",
            times=[],
        ))
    days.sort(key=lambda x: x.day)
    return days


def _check_captcha(page) -> None:
    try:
        body = page.inner_text("body").lower()
    except Exception:  # noqa: BLE001
        return
    if any(marker in body for marker in CAPTCHA_MARKERS):
        raise CaptchaDetected("Captcha/Sicherheitsabfrage auf der Seite erkannt")


def _require(page, selector: str, what: str):
    loc = page.locator(selector)
    if loc.count() == 0:
        raise StructureError(f"{what} ({selector}) nicht gefunden")
    return loc


def check_once(settings: Settings) -> CycleResult:
    """Run exactly one check. Always returns a CycleResult (classified status)."""
    sid = settings.service_id
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=settings.headless, args=LAUNCH_ARGS)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1280, "height": 1300},
        )
        page = ctx.new_page()
        page.set_default_timeout(settings.nav_timeout_ms)
        try:
            page.goto(settings.base_url, wait_until="domcontentloaded")
            _check_captcha(page)

            # Cookie banner (reject, if present).
            try:
                btn = page.locator("#cookie_msg_btn_no")
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
            except Exception:  # noqa: BLE001
                pass

            # Schritt 1 -> 2: choose the functional unit (1 = Bürgerbüro, 2 = Führerscheinstelle).
            fe = page.locator(f"#buttonfunktionseinheit-{settings.funktionseinheit}")
            if fe.count() == 0:
                raise StructureError(f"Funktionseinheit-Button {settings.funktionseinheit} nicht gefunden")
            fe.first.click()
            page.wait_for_load_state("networkidle")
            _check_captcha(page)

            # Schritt 2: expand the Anliegen category (accordion) if configured, then
            # select the service (qty -> 1) and continue.
            if settings.anliegen_category:
                cat = page.locator("h3.ui-accordion-header", has_text=settings.anliegen_category)
                if cat.count() == 0:
                    raise StructureError(f"Anliegen-Kategorie '{settings.anliegen_category}' nicht gefunden")
                cat.first.click()

            plus = page.locator(f"#button-plus-{sid}")
            try:
                plus.wait_for(state="visible", timeout=10000)
            except PWTimeout as ex:
                raise StructureError(f"Anliegen {sid} nicht sichtbar (Kategorie nicht aufgeklappt?)") from ex
            plus.click()
            try:
                if page.locator(f"#input-{sid}").input_value() == "0":
                    plus.click()
            except Exception:  # noqa: BLE001
                pass
            _require(page, "#WeiterButton", "Weiter (Anliegen)").click()

            # Modal: confirm all required-document checkboxes, then OK.
            ok = page.locator("#OKButton")
            try:
                ok.wait_for(state="visible", timeout=settings.nav_timeout_ms)
            except PWTimeout as ex:
                raise StructureError("Hinweis-Modal (#OKButton) erschien nicht") from ex
            labels = page.locator("label.labelChecklist")
            n_labels = labels.count()
            if n_labels == 0:
                raise StructureError("Keine Checkbox-Labels (label.labelChecklist) im Modal")
            for i in range(n_labels):
                lab = labels.nth(i)
                lab.scroll_into_view_if_needed()
                lab.click()
            try:
                page.wait_for_selector("#OKButton:not([aria-disabled='true'])", timeout=8000)
            except PWTimeout as ex:
                raise StructureError("OK blieb deaktiviert (Checkboxen nicht alle aktivierbar)") from ex
            ok.click()

            # Schritt 3: location step. Each location is an accordion header whose text
            # states the earliest free date ("... Termine ab DD.MM.YYYY ...").
            try:
                page.wait_for_function("() => /Schritt 3/.test(document.title)", timeout=settings.nav_timeout_ms)
            except PWTimeout:
                body = ""
                try:
                    body = page.inner_text("body").lower()
                except Exception:  # noqa: BLE001
                    pass
                if any(m in body for m in NO_SLOTS_MARKERS):
                    return CycleResult(status=STATUS_OK, days=[], message="Keine Termine angeboten.")
                raise StructureError(f"Schritt 3 nicht erreicht (Titel: {page.title()!r})")
            page.wait_for_load_state("networkidle")
            _check_captcha(page)

            headers = page.evaluate(LOCATION_HEADER_JS)
            days = parse_locations(headers)
            return CycleResult(
                status=STATUS_OK,
                days=days,
                message=f"{len(days)} Standort(e) mit Terminen.",
            )

        except CaptchaDetected as ex:
            return CycleResult(status=STATUS_CAPTCHA, message=str(ex))
        except StructureError as ex:
            return CycleResult(status=STATUS_STRUCTURE, message=str(ex))
        except (PWTimeout, PWError) as ex:
            return CycleResult(status=STATUS_NETWORK, message=f"{type(ex).__name__}: {str(ex)[:200]}")
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
