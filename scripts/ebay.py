"""eBay Browse API price snapshot for sets TCGdex doesn't cover yet.

Takes the *asking* prices of active fixed-price eBay US listings for each card,
drops listings that aren't a single raw copy of that card (lots, graded slabs,
other sets...), and records median / 25th percentile / lowest price and count.

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
CCG_SINGLES = "183454"  # eBay category: CCG Individual Cards

# A title containing any of these isn't a single raw card.
EXCLUDE = re.compile(
    r"\b(lot|lots|set|sets|bundle|complete|full|psa|cgc|bgs|beckett|sgc|graded|slab|"
    r"toy|toys|plush|figure|proxy|custom|fake|choose|pick|select|sealed|box|"
    r"x ?[2-9]|[2-9] ?x|\d+ ?cards)\b",
    re.I,
)
SET_WORDS = re.compile(r"mcdonald|mcd\b|happy meal|\bm26\b", re.I)


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


def search(tok, q, attempts=4):
    params = urllib.parse.urlencode({
        "q": q,
        "category_ids": CCG_SINGLES,
        "filter": "buyingOptions:{FIXED_PRICE},priceCurrency:USD,deliveryCountry:US",
        "limit": "200",
    })
    req = urllib.request.Request(f"{SEARCH_URL}?{params}", headers={
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


def matches(title, card, year, all_names):
    t = title.lower()
    if card["name"].lower() not in t or str(year) not in t or not SET_WORDS.search(t):
        return False
    if EXCLUDE.search(t):
        return False
    # Listings naming two cards from the set are multi-card listings.
    others = [n for n in all_names if n != card["name"].lower() and n in t]
    return not others


def stats(prices):
    prices = sorted(prices)
    q = statistics.quantiles(prices, n=4) if len(prices) >= 4 else [prices[0]] * 3
    return {
        "median": round(statistics.median(prices), 2),
        "p25": round(q[0], 2),
        "low": round(prices[0], 2),
        "count": len(prices),
    }


def best_image(items, card, year, all_names):
    """Photo from a matched listing, preferring titles with the exact card number (e.g. 004/015)."""
    matched = [it for it in items
               if it.get("image", {}).get("imageUrl") and matches(it.get("title", ""), card, year, all_names)]
    num = str(card.get("number", "")).zfill(3)
    matched.sort(key=lambda it: f"{num}/" not in it["title"])
    if not matched:
        return None
    it = matched[0]
    return {
        "image": re.sub(r"/s-l\d+\.", "/s-l500.", it["image"]["imageUrl"]),
        "imageLink": it.get("itemWebUrl"),
        "imageCredit": "eBay listing photo",
    }


def images_for(cards, year):
    """{card_id: image_info} for (card_id, card) pairs, one eBay search each."""
    tok = token(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    all_names = [c["name"].lower() for _, c in cards]
    out = {}
    for cid, card in cards:
        try:
            img = best_image(search(tok, f"{card['name']} mcdonalds {year}"), card, year, all_names)
        except Exception as e:  # noqa: BLE001
            print(f"  ! image {cid}: {e}")
            continue
        if img:
            out[cid] = img
        time.sleep(0.5)
    return out


def snapshot(ebay_set, today):
    """Return {card_id: (card_meta, point_or_None)} for one configured set."""
    tok = token(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    year = ebay_set["year"]
    all_names = [c["name"].lower() for c in ebay_set["cards"]]
    out = {}
    for card in ebay_set["cards"]:
        cid = f"{ebay_set['id']}-{card['number']}"
        meta = {"name": card["name"], "set": ebay_set["id"], "number": card["number"],
                "rarity": "Holo" if card.get("holo") else "Regular"}
        try:
            items = search(tok, f"{card['name']} mcdonalds {year}")
        except Exception as e:  # noqa: BLE001
            print(f"  ! {cid}: {e}")
            out[cid] = (meta, None)
            continue
        seen, prices = set(), []
        for it in items:
            if it.get("itemId") in seen or not matches(it.get("title", ""), card, year, all_names):
                continue
            seen.add(it.get("itemId"))
            try:
                prices.append(float(it["price"]["value"]))
            except (KeyError, ValueError):
                pass
        meta.update(best_image(items, card, year, all_names) or {})
        point = {"date": today, **stats(prices)} if prices else None
        print(f"  {cid} {card['name']}: {len(prices)}/{len(items)} listings kept"
              + (f", median ${point['median']}" if point else ""))
        out[cid] = (meta, point)
        time.sleep(0.5)
    return out
