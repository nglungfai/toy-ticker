#!/usr/bin/env python3
"""Fetch today's Cardmarket prices for McDonald's Pokémon promo cards from TCGdex
and append them to data/prices.json. Safe to run more than once a day: the
latest run for a date replaces that date's entry.

Standard library only (Python 3.9+).
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.tcgdex.net/v2/en"
ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "prices.json"
CONFIG_FILE = ROOT / "data" / "config.json"

# Price fields kept from TCGdex's `pricing.cardmarket` block (EUR).
FIELDS = ["trend", "avg", "low", "avg1", "avg7", "avg30",
          "trend-holo", "avg-holo", "low-holo"]


def get(path, attempts=5):
    """GET a TCGdex endpoint, retrying temporary failures (5xx, timeouts) with backoff."""
    req = urllib.request.Request(API + path, headers={"User-Agent": "mcd-pokemon-price-tracker"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if i == attempts - 1:
                raise
        time.sleep(2 ** i * 3)


def pick_sets(config):
    """Explicit set IDs from config, plus every McDonald's set from `auto_from_year` on."""
    wanted = list(config.get("sets", []))
    year_from = config.get("auto_from_year")
    if year_from:
        try:
            all_sets = get("/sets")
        except Exception as e:  # noqa: BLE001 - new-set discovery is optional
            print(f"! couldn't list sets, tracking configured sets only: {e}", file=sys.stderr)
            all_sets = []
        for s in all_sets:
            m = re.match(r"McDonald's Collection (\d{4})$", s.get("name", ""))
            if m and int(m.group(1)) >= year_from and s["id"] not in wanted:
                wanted.append(s["id"])
    return wanted


def main():
    config = json.loads(CONFIG_FILE.read_text())
    data = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {"sets": {}, "cards": {}, "history": {}}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    failures = 0
    for set_id in pick_sets(config):
        try:
            s = get(f"/sets/{set_id}")
        except Exception as e:  # noqa: BLE001
            print(f"! set {set_id}: {e}", file=sys.stderr)
            failures += 1
            continue
        data["sets"][set_id] = {"name": s["name"], "releaseDate": s.get("releaseDate")}
        print(f"{s['name']}: {len(s['cards'])} cards")

        for c in s["cards"]:
            try:
                card = get(f"/cards/{c['id']}")
            except Exception as e:  # noqa: BLE001
                print(f"  ! {c['id']}: {e}", file=sys.stderr)
                failures += 1
                continue
            cm = (card.get("pricing") or {}).get("cardmarket") or {}
            data["cards"][card["id"]] = {
                "name": card["name"],
                "set": set_id,
                "number": card.get("localId"),
                "rarity": card.get("rarity"),
                "image": card.get("image"),
                "cardmarketId": cm.get("idProduct"),
            }
            if not cm:
                continue
            point = {"date": today, **{k: cm.get(k) for k in FIELDS if cm.get(k) is not None}}
            hist = [p for p in data["history"].get(card["id"], []) if p["date"] != today]
            hist.append(point)
            hist.sort(key=lambda p: p["date"])
            data["history"][card["id"]] = hist

    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["currency"] = "EUR"
    data["source"] = "Cardmarket via TCGdex (api.tcgdex.net)"
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"Saved {len(data['history'])} cards with prices -> {DATA_FILE.relative_to(ROOT)}")
    return 1 if failures and not data["history"] else 0


if __name__ == "__main__":
    sys.exit(main())
