#!/usr/bin/env python3
"""Record today's eBay asking prices for each Happy Meal series in data/config.json.

Data layout (so the page stays fast as the catalogue grows):
  data/index.json            every series and item, plus each item's latest price summary
  data/series/<series>.json  full daily price history for one series (loaded on demand)

Current and recently ended series refresh daily; series that ended more than
WEEKLY_AFTER_DAYS ago refresh weekly to save eBay API calls. Safe to run more than once
a day: the latest run for a date replaces that date's entry.

Standard library only (Python 3.9+); link-preview images additionally need Pillow.
"""
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ebay  # noqa: E402
import listings  # noqa: E402
import official_images  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
INDEX_FILE = DATA_DIR / "index.json"
SERIES_DIR = DATA_DIR / "series"
LEGACY_FILE = DATA_DIR / "prices.json"  # pre-split single file; migrated automatically
CONFIG_FILE = DATA_DIR / "config.json"
SERIES_KEYS = ("name", "description", "kind", "start", "end", "current", "note", "franchise")
WEEKLY_AFTER_DAYS = 90


def merge_meta(old, new):
    """New item metadata, but keep an already-chosen image so it doesn't change every day
    (unless it was chosen by an older version of the photo picker)."""
    old = old or {}
    merged = {**new}
    if old.get("image") and old.get("imageV", 1) >= new.get("imageV", 1):
        for k in ("image", "imageLink", "imageCredit", "imageV"):
            if k in old:
                merged[k] = old[k]
    return merged


def load_data():
    """Everything in one dict {sets, cards, history, calendar, updated}, from split files or the legacy file."""
    if INDEX_FILE.exists():
        idx = json.loads(INDEX_FILE.read_text())
        data = {"sets": idx.get("sets", {}), "cards": idx.get("items", {}), "history": {}, "sold": {},
                "calendar": idx.get("calendar", []), "updated": idx.get("updated")}
        for sid in data["sets"]:
            f = SERIES_DIR / f"{sid}.json"
            if f.exists():
                saved = json.loads(f.read_text())
                data["history"].update(saved.get("history", {}))
                data["sold"].update(saved.get("sold", {}))
        return data
    if LEGACY_FILE.exists():
        return json.loads(LEGACY_FILE.read_text())
    return {"sets": {}, "cards": {}, "history": {}}


def summary(hist):
    """Latest price facts for the index: enough for search, ticker, map and calendar without history."""
    if not hist:
        return None
    last, prev = hist[-1], (hist[-2] if len(hist) > 1 else None)
    out = {k: last[k] for k in ("date", "median", "p25", "p75", "low", "count", "watchers", "watched", "cond", "graded", "total", "supply") if k in last}
    if prev and prev.get("median") and (date.fromisoformat(last["date"]) - date.fromisoformat(prev["date"])).days <= 2:
        out["d1"] = round(last["median"] / prev["median"] - 1, 4)  # weekly series have no 1-day change
    month_ago = [p for p in hist[:-1] if (date.fromisoformat(last["date"]) - date.fromisoformat(p["date"])).days <= 30]
    vals = [p["median"] for p in month_ago if p.get("median")]
    if vals:
        out["d30"] = round(last["median"] / (sum(vals) / len(vals)) - 1, 4)
    return out


def save_data(data):
    """Write index.json and one history file per series; remove the legacy single file."""
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    items = {}
    for iid, c in data["cards"].items():
        s = summary(data["history"].get(iid))
        items[iid] = {**c, **({"latest": s} if s else {})}
    for sid, s in data["sets"].items():
        days = {p["date"] for iid, c in data["cards"].items() if c.get("set") == sid for p in data["history"].get(iid, [])}
        s["days"] = len(days)
        s["lastUpdate"] = max(days) if days else None
        hist = {iid: h for iid, h in data["history"].items() if data["cards"].get(iid, {}).get("set") == sid}
        # Weekly sold medians from the listing ledger (listings.sold_weeks), for the chart's sold line.
        sold = {iid: w for iid, w in data.get("sold", {}).items()
                if w and (data["cards"].get(iid, {}).get("set") == sid or iid == f"{sid}-SET")}
        (SERIES_DIR / f"{sid}.json").write_text(json.dumps({"series": sid, "history": hist, **({"sold": sold} if sold else {})},
                                                           ensure_ascii=False) + "\n")
    for f in SERIES_DIR.glob("*.json"):  # series no longer configured
        if f.stem not in data["sets"]:
            f.unlink()
    INDEX_FILE.write_text(json.dumps({"sets": data["sets"], "items": items, "groups": data.get("groups", []),
                                      "calendar": data.get("calendar", []),
                                      "updated": data.get("updated")}, indent=1, ensure_ascii=False) + "\n")
    if LEGACY_FILE.exists():
        LEGACY_FILE.unlink()


def is_weekly(series, today):
    """Series that ended more than WEEKLY_AFTER_DAYS ago only need a weekly price check."""
    return bool(series.get("end") and not series.get("current")
                and (date.fromisoformat(today) - date.fromisoformat(series["end"])).days > WEEKLY_AFTER_DAYS)


def due(series, last_update, today):
    """Daily series are always due; weekly ones when their last snapshot is 7+ days old."""
    return (os.environ.get("REFRESH_ALL") == "1" or not last_update or not is_weekly(series, today)
            or (date.fromisoformat(today) - date.fromisoformat(last_update)).days >= 7)


def main():
    config = json.loads(CONFIG_FILE.read_text())
    data = load_data()
    for k in ("sets", "cards", "history"):
        data.setdefault(k, {})
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

    failures = calls = 0
    for order, s in enumerate(config["series"]):
        prev = data["sets"].get(s["id"], {})
        last_update = prev.get("lastUpdate")
        data["sets"][s["id"]] = {**{k: prev[k] for k in ("fullSet", "insights", "backfill") if prev.get(k)},
                                 **{k: s[k] for k in SERIES_KEYS if k in s}, "order": order,
                                 "refresh": "weekly" if is_weekly(s, today) else "daily",
                                 "currency": "USD", "source": "eBay US active listings (median asking price)"}
        if not have_keys:
            for it in s["items"]:
                iid = f"{s['id']}-{it['number']}"
                data["cards"][iid] = merge_meta(data["cards"].get(iid), ebay.item_meta(s, it))
            continue
        if not due(s, last_update, today):
            print(f"{s['name']}: weekly series, last updated {last_update}, skipping today")
            continue
        print(f"{s['name']}: {len(s['items'])} items")
        try:
            results = ebay.snapshot(s, today)
            calls += len(s["items"]) * (3 if s.get("kind") == "cards" and s.get("graded") else 2)  # + auctions
        except Exception as e:  # noqa: BLE001
            print(f"! {s['id']}: {e}", file=sys.stderr)
            failures += 1
            continue
        for iid, (meta, point) in results.items():
            data["cards"][iid] = merge_meta(data["cards"].get(iid), meta)
            if point:
                hist = [p for p in data["history"].get(iid, []) if p["date"] != today]
                data["history"][iid] = sorted(hist + [point], key=lambda p: p["date"])
        # Complete-set listings: one extra search per series, kept as a small daily history on the series.
        try:
            fs = ebay.full_set(s, today)
            calls += 1
            prev_fs = data["sets"][s["id"]].get("fullSet", {})
            hist = [p for p in prev_fs.get("history", []) if p["date"] != today]
            if fs:
                hist.append(fs["point"])
                data["sets"][s["id"]]["fullSet"] = {"history": sorted(hist, key=lambda p: p["date"]), "deals": fs["deals"],
                                                    "size": fs["size"], "search": fs["search"]}
            elif prev_fs:
                data["sets"][s["id"]]["fullSet"] = {**prev_fs, "history": hist, "deals": []}
        except Exception as e:  # noqa: BLE001
            print(f"! {s['id']} full set: {e}", file=sys.stderr)
        # Listing ledger: follow matched listings over time to estimate sold prices.
        try:
            led = listings.update(s["id"], today)
            calls += (led or {}).get("checked", 0)
            data["sets"][s["id"]]["insights"] = listings.insights(s["id"], today)
            after = (data["sets"][s["id"]].get("backfill") or {}).get("to")
            data.setdefault("sold", {}).update(listings.sold_weeks(s["id"], today, after))
        except Exception as e:  # noqa: BLE001
            print(f"! {s['id']} listing ledger: {e}", file=sys.stderr)

    # Official toy photos (series with "official_images") take priority over eBay listing photos.
    for s in config["series"]:
        for iid, m in official_images.collect(s).items():
            if iid in data["cards"]:
                data["cards"][iid].update(m)

    # Happy Meal calendar: tracked series come from data["sets"]; this adds untracked and upcoming ones.
    data["calendar"] = config.get("calendar", [])
    # Groups: series the site shows as one (members keep their own data).
    data["groups"] = [g for g in config.get("groups", []) if all(m in data["sets"] for m in g["members"])]
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_data(data)
    print(f"Saved {len(data['history'])} items with prices across {len(data['sets'])} series "
          f"({calls} eBay API calls) -> data/index.json + data/series/")

    if os.environ.get("MATCH_REPORT") and ebay.REPORT:
        Path(os.environ["MATCH_REPORT"]).write_text(json.dumps(ebay.REPORT, indent=1, ensure_ascii=False))
        print(f"Match report: {os.environ['MATCH_REPORT']}")

    # Link-preview images and share pages (optional: needs Pillow).
    try:
        import og
        built = og.build(data, ROOT, config.get("site_url", ""))
        print(f"Link previews: og/site.png + {len(built)} series")
    except ImportError:
        print("! Pillow not installed: skipping link-preview images", file=sys.stderr)

    # Search pages (static HTML per series and item, sitemap, JSON-LD).
    import seo
    n_series, n_items = seo.build(data, ROOT, config.get("site_url", ""))
    print(f"Search pages: {n_series} series + {n_items} items -> s/, i/, sitemap.xml")
    return 1 if failures and failures == len(config["series"]) else 0


if __name__ == "__main__":
    sys.exit(main())
