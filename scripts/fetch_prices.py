#!/usr/bin/env python3
"""Fetch today's Cardmarket prices for McDonald's Pokémon promo cards from TCGdex
and append them to data/prices.json. Safe to run more than once a day: the
latest run for a date replaces that date's entry.

Standard library only (Python 3.9+).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ebay  # noqa: E402

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


def merge_meta(old, new):
    """New card metadata, but keep an already-chosen image so it doesn't change every day."""
    old = old or {}
    merged = {**new}
    if old.get("image"):
        for k in ("image", "imageLink", "imageCredit"):
            if k in old:
                merged[k] = old[k]
    return merged


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
        data["sets"][set_id] = {"name": s["name"], "releaseDate": s.get("releaseDate"),
                                "currency": "EUR", "source": "Cardmarket via TCGdex"}
        print(f"{s['name']}: {len(s['cards'])} cards")

        for c in s["cards"]:
            try:
                card = get(f"/cards/{c['id']}")
            except Exception as e:  # noqa: BLE001
                print(f"  ! {c['id']}: {e}", file=sys.stderr)
                failures += 1
                continue
            cm = (card.get("pricing") or {}).get("cardmarket") or {}
            meta = {
                "name": card["name"],
                "set": set_id,
                "number": card.get("localId"),
                "rarity": card.get("rarity"),
                "cardmarketId": cm.get("idProduct"),
            }
            if card.get("image"):  # TCGdex gives a base URL; quality/format are path suffixes
                meta.update(image=card["image"] + "/high.webp", imageCredit="TCGdex",
                            imageLink="https://tcgdex.dev")
            data["cards"][card["id"]] = merge_meta(data["cards"].get(card["id"]), meta)
            if not cm:
                continue
            point = {"date": today, **{k: cm.get(k) for k in FIELDS if cm.get(k) is not None}}
            hist = [p for p in data["history"].get(card["id"], []) if p["date"] != today]
            hist.append(point)
            hist.sort(key=lambda p: p["date"])
            data["history"][card["id"]] = hist

    have_keys = bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))

    # TCGdex has no scans for some promo sets: borrow an eBay listing photo, once per card.
    if have_keys:
        missing = {}
        for cid, c in data["cards"].items():
            year = (data["sets"].get(c["set"], {}).get("releaseDate") or "")[:4]
            if not c.get("image") and data["sets"].get(c["set"], {}).get("currency") == "EUR" and year:
                missing.setdefault(year, []).append((cid, c))
        for year, cards in missing.items():
            print(f"Looking up eBay photos for {len(cards)} cards from {year}")
            try:
                for cid, img in ebay.images_for(cards, int(year)).items():
                    data["cards"][cid].update(img)
            except Exception as e:  # noqa: BLE001
                print(f"! eBay images: {e}", file=sys.stderr)

    # Sets TCGdex doesn't have yet: eBay active-listing prices (USD).
    for es in config.get("ebay_sets", []):
        print(f"{es['name']} (eBay): {len(es['cards'])} cards")
        data["sets"][es["id"]] = {"name": es["name"], "releaseDate": es.get("releaseDate"),
                                  "currency": "USD", "source": "eBay US active listings (median asking price)"}
        if not have_keys:
            # Still publish the card list so the page shows it, just without prices.
            print("! EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set, no eBay prices", file=sys.stderr)
            for c in es["cards"]:
                data["cards"].setdefault(f"{es['id']}-{c['number']}", {
                    "name": c["name"], "set": es["id"], "number": c["number"],
                    "rarity": "Holo" if c.get("holo") else "Regular"})
            continue
        try:
            results = ebay.snapshot(es, today)
        except Exception as e:  # noqa: BLE001
            print(f"! eBay set {es['id']}: {e}", file=sys.stderr)
            failures += 1
            continue
        for cid, (meta, point) in results.items():
            data["cards"][cid] = merge_meta(data["cards"].get(cid), meta)
            if point:
                hist = [p for p in data["history"].get(cid, []) if p["date"] != today]
                data["history"][cid] = sorted(hist + [point], key=lambda p: p["date"])

    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data.pop("currency", None)
    data.pop("source", None)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"Saved {len(data['history'])} cards with prices -> {DATA_FILE.relative_to(ROOT)}")
    return 1 if failures and not data["history"] else 0


if __name__ == "__main__":
    sys.exit(main())
