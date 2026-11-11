#!/usr/bin/env python3
"""Record today's eBay asking prices for each Happy Meal series in data/config.json
and append them to data/prices.json. Safe to run more than once a day: the latest
run for a date replaces that date's entry.

Standard library only (Python 3.9+).
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ebay  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "prices.json"
CONFIG_FILE = ROOT / "data" / "config.json"
SERIES_KEYS = ("name", "kind", "start", "end", "current", "note")


def merge_meta(old, new):
    """New item metadata, but keep an already-chosen image so it doesn't change every day."""
    old = old or {}
    merged = {**new}
    if old.get("image"):
        for k in ("image", "imageLink", "imageCredit"):
            if k in old:
                merged[k] = old[k]
    return merged


def main():
    config = json.loads(CONFIG_FILE.read_text())
    data = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}
    data.setdefault("sets", {})
    data.setdefault("cards", {})
    data.setdefault("history", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    have_keys = bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))
    if not have_keys:
        print("! EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set: publishing item lists without prices",
              file=sys.stderr)

    # Drop series that are no longer configured.
    wanted = {s["id"] for s in config["series"]}
    data["sets"] = {k: v for k, v in data["sets"].items() if k in wanted}
    data["cards"] = {k: v for k, v in data["cards"].items() if v.get("set") in wanted}
    data["history"] = {k: v for k, v in data["history"].items() if k in data["cards"]}

    failures = 0
    for order, s in enumerate(config["series"]):
        print(f"{s['name']}: {len(s['items'])} items")
        data["sets"][s["id"]] = {**{k: s[k] for k in SERIES_KEYS if k in s}, "order": order,
                                 "currency": "USD", "source": "eBay US active listings (median asking price)"}
        if not have_keys:
            for it in s["items"]:
                iid = f"{s['id']}-{it['number']}"
                data["cards"][iid] = merge_meta(data["cards"].get(iid), ebay.item_meta(s, it))
            continue
        try:
            results = ebay.snapshot(s, today)
        except Exception as e:  # noqa: BLE001
            print(f"! {s['id']}: {e}", file=sys.stderr)
            failures += 1
            continue
        for iid, (meta, point) in results.items():
            data["cards"][iid] = merge_meta(data["cards"].get(iid), meta)
            if point:
                hist = [p for p in data["history"].get(iid, []) if p["date"] != today]
                data["history"][iid] = sorted(hist + [point], key=lambda p: p["date"])

    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"Saved {len(data['history'])} items with prices -> {DATA_FILE.relative_to(ROOT)}")

    # Link-preview images and share pages (optional: needs Pillow).
    try:
        import og
        built = og.build(data, ROOT, config.get("site_url", ""))
        print(f"Link previews: og/site.png + {len(built)} series")
    except ImportError:
        print("! Pillow not installed: skipping link-preview images", file=sys.stderr)
    return 1 if failures == len(config["series"]) else 0


if __name__ == "__main__":
    sys.exit(main())
