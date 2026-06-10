#!/usr/bin/env python3
"""Diagnose-Tool: einmal den kompletten Buchungs-Flow prüfen und ausführlich
anzeigen, was gefunden wurde. Nutze es, wenn der Monitor einen 'structure_error'
meldet — die Statusmeldung zeigt genau, an welchem Schritt es hakt.

  .venv/bin/python scripts/diagnose.py            # headless (wie im Betrieb)
  .venv/bin/python scripts/diagnose.py --headed   # sichtbarer Browser zum Zuschauen
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Flow der Buchungsseite einmal diagnostizieren.")
    parser.add_argument("--headed", action="store_true", help="sichtbaren Browser öffnen")
    args = parser.parse_args()
    if args.headed:
        os.environ["HEADLESS"] = "false"

    from src.config import load_settings
    from src.scraper import check_once

    settings = load_settings()
    print(f"→ Prüfe {settings.base_url}  (headless={settings.headless}, Anliegen-ID={settings.service_id})\n")

    res = check_once(settings)
    print(f"Status : {res.status}")
    print(f"Meldung: {res.message}")
    if res.days:
        print("\nAngebotene Tage:")
        for d in res.days:
            preview = ", ".join(d.times[:6]) + ("…" if len(d.times) > 6 else "")
            print(f"  • {d.label}   ({len(d.times)} Zeiten)   {preview}")
        in_window = [d for d in res.available_dates if settings.target_start <= d <= settings.target_end]
        print(f"\nZielfenster {settings.target_start}–{settings.target_end}: "
              f"{[d.isoformat() for d in in_window] or '— nichts —'}")

    if res.status != "ok":
        print("\n⚠  Kein OK. Obige Meldung zeigt den Schritt, an dem der Ablauf hängt.")
        return 1
    print("\n✅ Flow OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
