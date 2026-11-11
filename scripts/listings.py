"""Listing ledger: follow every matched eBay listing from day to day to estimate *sold* prices.

eBay's Browse API only shows active listings, so sold prices aren't available directly. Instead:
  1. Every listing we match is recorded in data/listings/<series>.json with its latest delivered price.
  2. When a listing stops appearing in our searches, we look it up (getItem, COMPACT). Searches are
     capped at 200 results, so a missing listing has often just slipped down the results.
       - sold out with sales     -> "sold" at its last delivered price (item + shipping)
       - sold out with no sales  -> "ended" (the seller ended it unsold)
       - still for sale          -> stays active; its sold quantity is recorded, and any increase
                                    after that is a "sold" event (multi-quantity listings)
       - not found (404)         -> "gone" (removed; we can't tell why)
  3. Events are kept as a history. Nothing is shown on the site until there's enough to be useful.

The file is written one listing/event per line so daily git diffs stay small.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import ebay

ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/"
DIR = Path(__file__).resolve().parent.parent / "data" / "listings"
CHECK_BUDGET = 1500   # getItem lookups per run (Browse API allows a few thousand calls a day)
RECHECK_DAYS = 2      # a missing listing that's still for sale is looked up again after this many days
DROP_AFTER_DAYS = 21  # stop following a listing that hasn't appeared in our searches for this long
_budget = {"left": CHECK_BUDGET}


def days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def load(sid):
    f = DIR / f"{sid}.json"
    if f.exists():
        d = json.loads(f.read_text())
        return d.get("active", {}), d.get("events", [])
    return {}, []


def save(sid, active, events, today):
    DIR.mkdir(parents=True, exist_ok=True)
    line = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    act = ",\n".join(f"  {line(k)}:{line(v)}" for k, v in sorted(active.items(), key=lambda kv: (kv[1]["key"], kv[0])))
    ev = ",\n".join(f"  {line(e)}" for e in events)
    (DIR / f"{sid}.json").write_text(f'{{"series":{line(sid)},"updated":{line(today)},\n"active":{{\n{act}\n}},\n"events":[\n{ev}\n]}}\n')


def get_item(item_id):
    """(http_status, compact item or None) for one listing."""
    req = urllib.request.Request(f"{ITEM_URL}{urllib.parse.quote(item_id, safe='')}?fieldgroups=COMPACT", headers={
        "Authorization": f"Bearer {ebay.get_token()}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "X-EBAY-C-ENDUSERCTX": "contextualLocation=" + urllib.parse.quote(f"country=US,zip={ebay.SHIP_TO_ZIP}"),
    })
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return 200, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return e.code, None
            if (e.code < 500 and e.code != 429) or i == 2:
                return e.code, None
        except (urllib.error.URLError, TimeoutError):
            if i == 2:
                return 0, None
        time.sleep(2 ** i * 2)


def availability(it):
    ea = (it.get("estimatedAvailabilities") or [{}])[0]
    sold = ea.get("estimatedSoldQuantity")
    return ea.get("estimatedAvailabilityStatus"), (int(sold) if sold is not None else None)


def update(sid, today):
    """Merge today's matched listings for one series into its ledger and look up the missing ones."""
    run = ebay.SEEN.get(sid)
    if not run:
        return None
    active, events = load(sid)
    for key, it, price, ship, total in run["rows"]:
        lid = it.get("itemId")
        if not lid:
            continue
        old = active.get(lid, {})
        active[lid] = {"key": key, "title": (it.get("title") or "")[:90], "legacy": it.get("legacyItemId"),
                       "listed": (it.get("itemCreationDate") or today)[:10], "first": old.get("first", today),
                       "seen": today, "price": round(price, 2), "ship": round(ship, 2), "total": total,
                       **({"seller": it["seller"]["username"]} if (it.get("seller") or {}).get("username") else {}),
                       **({"watch": ebay.watchers(it)} if ebay.watchers(it) is not None else {}),
                       **({"sold": old["sold"]} if "sold" in old else {})}
    # Still in the raw results (e.g. now rejected by a stricter rule): it's listed, so not missing.
    for lid in run["raw"] & active.keys():
        active[lid]["seen"] = today
    missing = [lid for lid, a in active.items() if a["seen"] < today
               and days(a.get("checked", "2000-01-01"), today) >= RECHECK_DAYS]
    missing.sort(key=lambda lid: active[lid]["seen"])  # longest-missing first
    missing = missing[:max(0, _budget["left"])]
    _budget["left"] -= len(missing)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(get_item, missing))
    counts = {"tracked": len(active), "checked": len(missing), "sold": 0, "ended": 0, "gone": 0, "listed": 0, "error": 0}
    samples = []
    for lid, (code, it) in zip(missing, results):
        a = active[lid]
        base = {"date": today, "key": a["key"], "legacy": a.get("legacy"), "title": a["title"],
                "listed": a.get("listed"), "first": a["first"], "seen": a["seen"]}
        if code in (404, 410):
            events.append({**base, "kind": "gone", "total": a["total"]})
            del active[lid]; counts["gone"] += 1
            continue
        if code != 200:
            counts["error"] += 1
            continue
        status, sold = availability(it)
        price = ebay.item_price(it)
        ship = ebay.ship_cost(it)
        total = round((price if price is not None else a["price"]) + (ship if ship is not None else a["ship"]), 2)
        if len(samples) < 40:
            samples.append({"title": a["title"], "status": status, "sold": sold, "was": a.get("sold"), "total": total})
        new_sales = None if sold is None else (sold - a["sold"] if "sold" in a else sold)
        if status == "OUT_OF_STOCK":
            kind = "sold" if (new_sales or 0) > 0 else "ended"
            events.append({**base, "kind": kind, "total": total, **({"qty": new_sales} if kind == "sold" else {})})
            del active[lid]; counts[kind] += 1
        else:
            if "sold" in a and sold is not None and sold > a["sold"]:
                events.append({**base, "kind": "sold", "total": total, "qty": sold - a["sold"]})
                counts["sold"] += 1
            a.update({"checked": today, "total": total, **({"sold": sold} if sold is not None else {})})
            counts["listed"] += 1
    for lid in [lid for lid, a in active.items() if days(a["seen"], today) > DROP_AFTER_DAYS]:
        del active[lid]
    save(sid, active, events, today)
    if os.environ.get("MATCH_REPORT"):
        ebay.REPORT.setdefault("_ledger", {})[sid] = {**counts, "samples": samples}
    print(f"  {sid} ledger: {counts['tracked']} listings, {counts['checked']} looked up -> "
          f"{counts['sold']} sold, {counts['ended']} ended, {counts['gone']} gone, {counts['listed']} still listed"
          + (f", {counts['error']} errors" if counts["error"] else ""))
    return counts


def insights(sid, today, window=30):
    """Series facts from the ledger for the site: seller concentration and how fast listings sell.
    Seller names never leave the ledger; the site only gets shares."""
    active, events = load(sid)
    live = [a for a in active.values() if a["seen"] == today]
    out = {"since": min([a["first"] for a in active.values()] + [e["first"] for e in events], default=today)}
    by_seller = {}
    for a in live:
        if a.get("seller"):
            by_seller[a["seller"]] = by_seller.get(a["seller"], 0) + 1
    if by_seller:
        n = sum(by_seller.values())
        top = sorted(by_seller.values(), reverse=True)
        out["sellers"] = {"listings": n, "sellers": len(top), "top1": round(top[0] / n, 3),
                          "top3": round(sum(top[:3]) / n, 3), "top10": round(sum(top[:10]) / n, 3),
                          "one": round(sum(1 for c in top if c == 1) / len(top), 3)}
    recent = [e for e in events if days(e["date"], today) <= window]
    per = {}
    for e in recent:
        p = per.setdefault(e["key"], {"sold": 0, "ended": 0, "gone": 0, "days": []})
        if e["kind"] == "sold":
            p["sold"] += e.get("qty", 1) or 1
            if e.get("listed"):
                p["days"].append(max(0, days(e["listed"], e["date"])))
        elif e["kind"] in ("ended", "gone"):
            p[e["kind"]] += 1
    def summ(p):
        closed = p["sold"] + p["ended"] + p["gone"]
        d = sorted(p["days"])
        return {"sold": p["sold"], "closed": closed, **({"rate": round(p["sold"] / closed, 3)} if closed else {}),
                **({"days": d[len(d) // 2]} if d else {})}
    if per:
        total = {"sold": 0, "ended": 0, "gone": 0, "days": []}
        for p in per.values():
            for k in ("sold", "ended", "gone"):
                total[k] += p[k]
            total["days"] += p["days"]
        out["sales"] = {"window": window, **summ(total), "items": {k: summ(p) for k, p in per.items()}}
    return out
