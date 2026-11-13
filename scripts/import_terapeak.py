"""Backfill past *sold* prices for a series from a Terapeak export (eBay Seller Hub > Research > Product research).

Our daily data only starts when tracking began and records asking prices of live listings. Terapeak keeps
about three years of sold listings, so a one-off export fills in a series' history before that.

Usage:
  python3 scripts/import_terapeak.py <series-id> <file.csv> [more files...] [--report report.txt]

Files: a CSV download or a table copied from the Sold tab and saved as .csv or .tsv (comma or tab separated).
Column names are matched loosely; it needs a title, a sold price and a sold date. Shipping, quantity sold
and format are used when present. Each row is one listing: its average sold price plus average shipping
counts once per unit sold, on its last sold date.

Rows are assigned to items with the same title rules as the daily prices (ebay.reject_reason), so lots,
other items and other years are left out. Complete sets go to the series' full-set backfill.

Output: data/backfill/<series-id>.json (the individual sales plus weekly sold medians), and a "backfill"
summary on the series in data/index.json so the site knows to load it. Re-importing an overlapping export
doesn't double count: rows are keyed by title, date and price.
"""
import csv
import hashlib
import io
import json
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import ebay

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "data" / "config.json"
INDEX_FILE = ROOT / "data" / "index.json"
OUT_DIR = ROOT / "data" / "backfill"

COLUMNS = {
    "title": ("listing", "title", "item title", "listing title", "item"),
    "price": ("avg sold price", "average sold price", "sold price", "price", "sale price", "item price"),
    "ship": ("avg shipping", "average shipping", "avg shipping cost", "shipping", "shipping cost", "shipping price"),
    "qty": ("total sold", "quantity sold", "qty sold", "items sold", "sold", "quantity", "qty"),
    "date": ("date last sold", "last date sold", "date sold", "sold date", "last sold", "end date", "date"),
    "fmt": ("format", "listing format", "listing type", "type"),
}
DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d %Y", "%d-%b-%Y")
MAX_QTY_WEIGHT = 10   # one multi-quantity listing shouldn't drown out everything else in a week
OUTLIER_LOW, OUTLIER_HIGH = 0.25, 4.0   # vs the item's overall median: likely a mismatched title


def find_columns(header):
    cols, norm = {}, [re.sub(r"[^a-z ]", "", h.lower()).strip() for h in header]
    for key, names in COLUMNS.items():
        for name in names:  # first alias that matches wins, so "avg sold price" beats a bare "sold"
            if name in norm and norm.index(name) not in cols.values():
                cols[key] = norm.index(name)
                break
    missing = [k for k in ("title", "price", "date") if k not in cols]
    if missing:
        sys.exit(f"Couldn't find column(s) {', '.join(missing)} in header: {header}")
    return cols


def money(s):
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", s or "")
    return float(m.group(0).replace(",", "")) if m else None


def parse_date(s):
    s = re.sub(r"\s+\d{1,2}:\d{2}.*$", "", (s or "").strip()).replace("Sept", "Sep")
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def read_rows(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    delim = "\t" if text.split("\n", 1)[0].count("\t") > text.split("\n", 1)[0].count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]
    if not rows:
        return []
    cols = find_columns(rows[0])
    get = lambda r, k: r[cols[k]].strip() if k in cols and cols[k] < len(r) else ""
    out = []
    for r in rows[1:]:
        qty = money(get(r, "qty"))
        out.append({"title": get(r, "title"), "price": money(get(r, "price")), "ship": money(get(r, "ship")) or 0.0,
                    "qty": int(qty) if qty and qty > 0 else 1, "date": parse_date(get(r, "date")),
                    "fmt": "auction" if "auction" in get(r, "fmt").lower() else "fixed"})
    return out


def week_of(d, start=None):
    """The week's Monday, but never before the promo started (a week that straddles launch day starts on it)."""
    d = date.fromisoformat(d)
    return max((d - timedelta(days=d.weekday())).isoformat(), min(start or "", d.isoformat()))


def weekly(sales, start=None):
    """Weekly sold medians, weighted by units sold, after dropping prices far from the item's overall median."""
    if not sales:
        return [], 0
    mid = statistics.median(s["price"] for s in sales)
    kept = [s for s in sales if OUTLIER_LOW * mid <= s["price"] <= OUTLIER_HIGH * mid]
    weeks = {}
    for s in kept:
        weeks.setdefault(week_of(s["date"], start), []).extend([s["price"]] * min(s["qty"], MAX_QTY_WEIGHT))
    return [{"date": w, "median": round(statistics.median(p), 2), "low": round(min(p), 2), "count": len(p)}
            for w, p in sorted(weeks.items())], len(sales) - len(kept)


def main(argv):
    report_path = None
    if "--report" in argv:
        i = argv.index("--report")
        report_path, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    if len(argv) < 2:
        sys.exit(__doc__)
    sid, files = argv[0], argv[1:]
    config = json.loads(CONFIG_FILE.read_text())
    series = next((s for s in config["series"] if s["id"] == sid), None)
    if not series:
        sys.exit(f"Unknown series {sid!r}. Known: {', '.join(s['id'] for s in config['series'])}")

    out_file = OUT_DIR / f"{sid}.json"
    old = json.loads(out_file.read_text()) if out_file.exists() else {}
    sales = {k: list(v.get("sales", [])) for k, v in old.get("items", {}).items()}
    if old.get("fullSet"):
        sales["fullSet"] = list(old["fullSet"].get("sales", []))
    seen = {s["k"] for v in sales.values() for s in v}

    size = (series.get("full_set") or {}).get("size")
    counts = {"rows": 0, "added": 0, "duplicate": 0, "unusable": 0, "unmatched": 0, "ambiguous": 0}
    log = []
    for path in files:
        for r in read_rows(path):
            counts["rows"] += 1
            if not r["title"] or r["price"] is None or not r["date"]:
                counts["unusable"] += 1
                log.append(f"UNUSABLE  {r['title'][:90]!r} (price/date missing or unreadable)")
                continue
            hits = [f"{sid}-{it['number']}" for it in series["items"] if ebay.matches(r["title"], series, it)]
            if not hits and size and ebay.set_reason(r["title"], series, size) is None:
                hits = ["fullSet"]
            if len(hits) != 1:
                counts["ambiguous" if hits else "unmatched"] += 1
                why = f"matches {', '.join(hits)}" if hits else "; ".join(
                    sorted({ebay.reject_reason(r["title"], series, it) or "" for it in series["items"]} - {""})[:3])
                log.append(f"{'AMBIGUOUS' if hits else 'SKIPPED'}  {r['title'][:90]!r}  ({why})")
                continue
            price = round(r["price"] + r["ship"], 2)
            key = hashlib.sha1(f"{r['title'].lower()}|{r['date']}|{price}".encode()).hexdigest()[:10]
            if key in seen:
                counts["duplicate"] += 1
                continue
            seen.add(key)
            sales.setdefault(hits[0], []).append({"k": key, "date": r["date"], "price": price, "qty": r["qty"], "fmt": r["fmt"]})
            counts["added"] += 1
            log.append(f"{hits[0]:>28}  {r['date']}  ${price:>7.2f} x{r['qty']}  {r['title'][:80]!r}")

    out = {"series": sid, "source": "Terapeak (eBay sold listings)", "imported": date.today().isoformat(),
           "note": "Sold prices incl. shipping. Daily site prices are asking prices, so the two aren't the same measure.",
           "items": {}}
    dropped = 0
    for key, v in sorted(sales.items()):
        v.sort(key=lambda s: (s["date"], s["k"]))
        weeks, d = weekly(v, series.get("start"))
        dropped += d
        entry = {"weeks": weeks, "sales": v}
        if key == "fullSet":
            out["fullSet"] = entry
        else:
            out["items"][key] = entry
    all_sales = [s for v in sales.values() for s in v]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # One week or sale per line keeps the file small and re-import diffs readable.
    line = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    sep = ",\n   "
    block = lambda e: ('{"weeks":[\n   ' + sep.join(map(line, e["weeks"])) + '\n  ],\n  "sales":[\n   '
                       + sep.join(map(line, e["sales"])) + '\n  ]}')
    parts = [f'{line(k)}:{line(out[k])}' for k in ("series", "source", "imported", "note")]
    parts.append('"items":{\n ' + ",\n ".join(f"{line(k)}:{block(v)}" for k, v in out["items"].items()) + "\n}")
    if "fullSet" in out:
        parts.append(f'"fullSet":{block(out["fullSet"])}')
    out_file.write_text("{" + ",\n".join(parts) + "}\n")

    if INDEX_FILE.exists() and all_sales:
        idx = json.loads(INDEX_FILE.read_text())
        if sid in idx.get("sets", {}):
            idx["sets"][sid]["backfill"] = {
                "source": "Terapeak", "from": min(s["date"] for s in all_sales), "to": max(s["date"] for s in all_sales),
                "sales": sum(s["qty"] for s in all_sales)}
            INDEX_FILE.write_text(json.dumps(idx, indent=1, ensure_ascii=False) + "\n")

    print(f"{sid}: {counts['rows']} rows -> {counts['added']} added, {counts['duplicate']} already imported, "
          f"{counts['unmatched']} not a single item or set, {counts['ambiguous']} matched several items, "
          f"{counts['unusable']} unreadable; {dropped} outlier prices left out of the weekly medians")
    for key in sorted(sales):
        v = sales[key]
        print(f"  {key:<30} {sum(s['qty'] for s in v):>5} sold  {v[0]['date']} .. {v[-1]['date']}" if v else f"  {key}: none")
    missing = [f"{sid}-{it['number']}" for it in series["items"] if f"{sid}-{it['number']}" not in sales]
    if missing:
        print(f"  no sales for: {', '.join(missing)}")
    if report_path:
        Path(report_path).write_text("\n".join(log) + "\n")
        print(f"Row-by-row report: {report_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
