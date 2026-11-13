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
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
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


def load(sid, with_auctions=False):
    f = DIR / f"{sid}.json"
    d = json.loads(f.read_text()) if f.exists() else {}
    out = (d.get("active", {}), d.get("events", []))
    return (*out, d.get("auctions", {})) if with_auctions else out


def save(sid, active, events, today, auctions=None):
    DIR.mkdir(parents=True, exist_ok=True)
    line = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    act = ",\n".join(f"  {line(k)}:{line(v)}" for k, v in sorted(active.items(), key=lambda kv: (kv[1]["key"], kv[0])))
    ev = ",\n".join(f"  {line(e)}" for e in events)
    auc = ",\n".join(f"  {line(k)}:{line(v)}" for k, v in sorted((auctions or {}).items(), key=lambda kv: (kv[1]["key"], kv[0])))
    (DIR / f"{sid}.json").write_text(f'{{"series":{line(sid)},"updated":{line(today)},\n"active":{{\n{act}\n}},\n'
                                      f'"auctions":{{\n{auc}\n}},\n"events":[\n{ev}\n]}}\n')


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
    active, events, auctions = load(sid, with_auctions=True)
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
    counts.update(update_auctions(run, auctions, events, today))
    save(sid, active, events, today, auctions)
    if os.environ.get("MATCH_REPORT"):
        ebay.REPORT.setdefault("_ledger", {})[sid] = {**counts, "samples": samples}
    print(f"  {sid} ledger: {counts['tracked']} listings, {counts['checked']} looked up -> "
          f"{counts['sold']} sold, {counts['ended']} ended, {counts['gone']} gone, {counts['listed']} still listed; "
          f"auctions: {counts['auctions']} open, {counts['auction_sold']} sold, {counts['auction_unsold']} unsold"
          + (f", {counts['error']} errors" if counts["error"] else ""))
    return counts


def update_auctions(run, auctions, events, today):
    """Track matching auctions; once an auction's end time has passed, look it up for its final bid.
    An auction that ended with bids (and no unmet reserve) is a real sale at that price."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for key, it in run.get("auctions", []):
        lid = it.get("itemId")
        if not lid:
            continue
        ship = ebay.ship_cost(it)
        old = auctions.get(lid, {})
        auctions[lid] = {"key": key, "title": (it.get("title") or "")[:90], "legacy": it.get("legacyItemId"),
                         "end": it.get("itemEndDate") or old.get("end"), "bid": ebay.bid_price(it), "bids": ebay.bid_count(it) or 0,
                         "ship": ship if ship is not None else old.get("ship"), "first": old.get("first", today), "seen": today}
    ended = [lid for lid, a in auctions.items() if a.get("end") and a["end"] < now]
    ended = ended[:max(0, _budget["left"])]
    _budget["left"] -= len(ended)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(get_item, ended))
    c = {"auction_sold": 0, "auction_unsold": 0}
    for lid, (code, it) in zip(ended, results):
        a = auctions[lid]
        if code not in (200, 404, 410):
            continue  # try again next run
        base = {"date": today, "key": a["key"], "legacy": a.get("legacy"), "title": a["title"], "first": a["first"], "seen": a["seen"], "ended": a["end"]}
        if code == 200:
            bids = ebay.bid_count(it) if ebay.bid_count(it) is not None else a["bids"]
            price = ebay.bid_price(it) or a["bid"]
            ship = ebay.ship_cost(it)
            ship = ship if ship is not None else (a.get("ship") or 0)
            sold = bids > 0 and it.get("reservePriceMet") is not False
            est = False
        else:  # no longer available: fall back to the last bid we saw
            bids, price, ship, sold, est = a["bids"], a["bid"], a.get("ship") or 0, a["bids"] > 0, True
        if sold and price:
            events.append({**base, "kind": "auction", "total": round(price + ship, 2), "bids": bids, **({"est": True} if est else {})})
            c["auction_sold"] += 1
        else:
            events.append({**base, "kind": "auction_unsold", "total": round((price or 0) + ship, 2), "bids": bids})
            c["auction_unsold"] += 1
        del auctions[lid]
    for lid in [lid for lid, a in auctions.items() if days(a["seen"], today) > DROP_AFTER_DAYS]:
        del auctions[lid]
    c["auctions"] = len(auctions)
    return c


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
        if e["kind"] not in ("sold", "ended", "gone"):
            continue  # auctions are summarised separately below
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
    auc = {}
    for e in recent:
        if e["kind"] == "auction":
            auc.setdefault(e["key"], []).append(e["total"])
    if auc:
        out["auctions"] = {"window": window, "sold": sum(len(v) for v in auc.values()),
                           "items": {k: {"n": len(v), "median": round(statistics.median(v), 2)} for k, v in auc.items()}}
    if per:
        total = {"sold": 0, "ended": 0, "gone": 0, "days": []}
        for p in per.values():
            for k in ("sold", "ended", "gone"):
                total[k] += p[k]
            total["days"] += p["days"]
        out["sales"] = {"window": window, **summ(total), "items": {k: summ(p) for k, p in per.items()}}
    return out


def sold_weeks(sid, today, after=None):
    """Weekly sold medians per item from the ledger's sales (fixed-price sales and won auctions), shipping included.

    Same method as the Terapeak backfill (import_terapeak.weekly), so the site's sold line carries on from it:
    only sales after `after` (the backfill's last day) count, and the first week starts the day after it.
    A sale is dated the day we detected it, which can trail the real sale by a day or two."""
    from import_terapeak import weekly  # noqa: PLC0415  (shared method, imported lazily to avoid a cycle)
    _, events = load(sid)
    sales = {}
    for e in events:
        if e["kind"] not in ("sold", "auction") or (after and e["date"] <= after):
            continue
        sales.setdefault(e["key"], []).append({"date": e["date"], "price": e["total"], "qty": e.get("qty", 1) or 1})
    # A week never starts before the data behind it: the day after the backfill, or the day tracking began.
    first_day = ((date.fromisoformat(after) + timedelta(days=1)).isoformat() if after
                 else min((e["first"] for e in events if e.get("first")), default=None))
    out = {}
    for key, s in sales.items():
        weeks, _ = weekly(s, first_day)
        for w in weeks:
            monday = date.fromisoformat(w["date"]) - timedelta(days=date.fromisoformat(w["date"]).weekday())
            w["end"] = min((monday + timedelta(days=6)).isoformat(), today)
            w["src"] = "ebay"
        out[key] = weeks
    return out
