"""Push notifications via ntfy (https://ntfy.sh or self-hosted)."""
from __future__ import annotations

import logging

import requests

from .config import Settings

log = logging.getLogger("fs_monitor.notify")


def _ascii(value: str) -> str:
    """HTTP header values must be latin-1 safe; ntfy decodes Title as UTF-8, so we
    keep titles plain ASCII and carry umlauts/emoji in the (UTF-8) body instead."""
    return value.encode("ascii", "replace").decode("ascii")


def ntfy_send(
    settings: Settings,
    *,
    title: str,
    message: str,
    priority: str = "default",
    tags: list[str] | None = None,
    click: str | None = None,
    actions: str | None = None,
    timeout: int = 15,
) -> bool:
    """Send one push. Returns True on success, False otherwise (never raises).

    `click` makes tapping the notification open that URL; `actions` is a raw ntfy
    Actions header (e.g. 'view, Label, https://…, clear=true') for tappable buttons.
    """
    url = f"{settings.ntfy_server}/{settings.ntfy_topic}"
    headers: dict[str, str] = {"Title": _ascii(title), "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    if actions:
        headers["Actions"] = actions
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    try:
        resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=timeout)
        resp.raise_for_status()
        log.info("ntfy gesendet: %s", title)
        return True
    except Exception as ex:  # noqa: BLE001 - notification must never crash the loop
        log.error("ntfy Fehler (%s): %s", url, ex)
        return False
