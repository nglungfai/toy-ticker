"""eBay Browse API price snapshots for Happy Meal collectibles (toys and cards).

For each item in a series, searches active fixed-price eBay US listings, keeps only
titles that are a single copy of that item (no lots, sets, graded slabs, other
items...), and records the median / 25th percentile / lowest *asking* price and
the listing count.

Needs EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (a free eBay developer "Production" keyset).
"""
import base64
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

# Never a single item, whatever the series.
EXCLUDE_ANY = re.compile(
    r"\b(lot|lots|bundle|complete|full set|sets?|set of|choose|pick|select|you pick|"
    r"custom|fake|proxy|replica|x ?[2-9]|[2-9] ?x|\d+ ?(toys|cards|pcs|pieces|figures))\b"
)
# Extra exclusions for raw single cards.
EXCLUDE_CARDS = re.compile(
    r"\b(psa|cgc|bgs|beckett|sgc|graded|slab|toy|toys|plush|figure|sealed|box)\b"
)


def norm(s):
    return re.sub(r"\s+", " ", s.lower().replace("’", "'").replace("‘", "'").replace("-", " "))


def has(t, phrase):
    """Whole-word match, so 'scar' doesn't hit 'scary' and 'nami' doesn't hit 'tsunami'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(norm(phrase)) + r"(?![a-z0-9])", t) is not None


def hits(t, groups):
    """True if every group has at least one alternative in the title."""
    return all(any(has(t, alt) for alt in g) for g in groups)


def item_hits(t, item):
    return hits(t, item["match"]) and not any(has(t, w) for w in item.get("not", []))


def matches(title, series, item):
    t = norm(title)
    if not hits(t, series.get("require", [])) or not item_hits(t, item):
        return False
    if EXCLUDE_ANY.search(t) or (series["kind"] == "cards" and EXCLUDE_CARDS.search(t)):
        return False
    if any(has(t, w) for w in series.get("exclude", [])):
        return False
    # A title that also fully matches another item is a multi-item listing.
    return not any(o is not item and item_hits(t, o) for o in series["items"])


def token(client_id, secret):
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        }).encode(),
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def search(tok, q, category=None, attempts=4):
    params = {
        "q": q,
        "filter": "buyingOptions:{FIXED_PRICE},priceCurrency:USD,deliveryCountry:US",
        "limit": "200",
    }
    if category:
        params["category_ids"] = category
    req = urllib.request.Request(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}", headers={
        "Authorization": f"Bearer {tok}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    })
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r).get("itemSummaries", [])
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429 or i == attempts - 1:
                raise
        time.sleep(2 ** i * 3)


def stats(prices):
    prices = sorted(prices)
    q = statistics.quantiles(prices, n=4) if len(prices) >= 4 else [prices[0]] * 3
    return {
        "median": round(statistics.median(prices), 2),
        "p25": round(q[0], 2),
        "low": round(prices[0], 2),
        "count": len(prices),
    }


def best_image(matched, item):
    """Photo from a matched listing, preferring titles with an item's `prefer` text (e.g. 004/015)."""
    withpic = [it for it in matched if it.get("image", {}).get("imageUrl")]
    prefer = [norm(p) for p in item.get("prefer", [])]
    withpic.sort(key=lambda it: not any(p in norm(it["title"]) for p in prefer))
    if not withpic:
        return {}
    it = withpic[0]
    return {
        "image": re.sub(r"/s-l\d+\.", "/s-l500.", it["image"]["imageUrl"]),
        "imageLink": it.get("itemWebUrl"),
        "imageCredit": "eBay listing photo",
    }


DEAL_CUTOFF = 0.80  # a "deal" asks at most 80% of today's median


def deals(matched, median, limit=5):
    """Today's cheapest matched listings that ask well under the median (with links back to eBay)."""
    out = []
    for it in matched:
        try:
            price = float(it["price"]["value"])
        except (KeyError, ValueError):
            continue
        if price <= median * DEAL_CUTOFF and it.get("itemWebUrl"):
            img = (it.get("image") or {}).get("imageUrl")
            out.append({"title": it.get("title", "")[:120], "price": round(price, 2),
                        "url": it["itemWebUrl"], "below": round(1 - price / median, 3),
                        **({"image": img} if img else {})})
    return sorted(out, key=lambda d: d["price"])[:limit]


def snapshot(series, today):
    """Return {item_id: (item_meta, point_or_None)} for one configured series."""
    tok = token(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    out = {}
    for item in series["items"]:
        iid = f"{series['id']}-{item['number']}"
        meta = item_meta(series, item)
        try:
            items = search(tok, item["q"], series.get("category"))
        except Exception as e:  # noqa: BLE001
            print(f"  ! {iid}: {e}")
            out[iid] = (meta, None)
            continue
        seen, matched = set(), []
        for it in items:
            if it.get("itemId") not in seen and matches(it.get("title", ""), series, item):
                seen.add(it.get("itemId"))
                matched.append(it)
        prices = []
        for it in matched:
            try:
                prices.append(float(it["price"]["value"]))
            except (KeyError, ValueError):
                pass
        meta.update(best_image(matched, item))
        point = {"date": today, **stats(prices)} if prices else None
        meta["deals"] = deals(matched, point["median"]) if point else []
        print(f"  {iid} {item['name']}: {len(prices)}/{len(items)} listings kept"
              + (f", median ${point['median']}" if point else ""))
        out[iid] = (meta, point)
        time.sleep(0.5)
    return out


def item_meta(series, item):
    return {"name": item["name"], "set": series["id"], "number": item["number"],
            "rarity": "Holo" if item.get("holo") else None}
