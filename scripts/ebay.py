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

# Every listing seen this run, for the listing ledger (scripts/listings.py):
# SEEN[series_id] = {"raw": {itemId of every search result}, "rows": [(item_key, listing, price, ship, total)]}
SEEN = {}


def seen_for(series_id):
    return SEEN.setdefault(series_id, {"raw": set(), "rows": []})

# Never a single item, whatever the series.
EXCLUDE_ANY = re.compile(
    r"\b(lot|lots|bundle|complete set|full set|sets?|set of|choose|pick|select|you pick|singles|your choice|"
    r"custom|fake|proxy|replica|x ?[2-9]|[2-9] ?x|\d+ ?(toys|cards|pcs|pieces|figures)|"
    r"empty|box only|bag only|no toy|toy not included|japan|japanese|"
    r"parody|fan made|fanmade|fan art|fanart|3d print(ed)?|print|poster print|decal|pin|pins|magnet|shirt|sticker sheet only)\b"
)
# Card numbers like "004/015": a different set size or card number means a different card.
CARDNUM = re.compile(r"\b0*(\d{1,3})\s*/\s*0*(\d{2,3})\b")
# Toy series: a listing about cards is a different product (cards are tracked separately or not at all).
EXCLUDE_TOYS = re.compile(r"\b(cards?|tcg|trading card|holo|foil)\b")
# "No box or cards" / "without cards" describe a toy-only listing; drop them before the card checks.
NEGATED = re.compile(r"\b(no|without|w/o|not including|does not include)\s+(box\s+(or|and|&|nor)\s+)?(cards?|tcg)\b")
YEAR = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
# Extra exclusions for raw single cards.
EXCLUDE_CARDS = re.compile(
    r"\b(psa|cgc|bgs|beckett|sgc|graded|slab|toy|toys|plush|figure|sealed|box)\b"
)


# Graded cards: "PSA 10", "CGC Gem Mint 9.5", "BGS 9". Grading words are stripped before the usual card rules run.
GRADE = re.compile(r"\b(psa|cgc|bgs|beckett|sgc|tag)\s*(?:gem\s*mint|gem\s*mt|gem|mint|pristine|black label)?\s*(10|9\.5|9|8\.5|8|7\.5|7|6|5)\b")
GRADE_WORDS = re.compile(r"\b(psa|cgc|bgs|beckett|sgc|tag|graded|slab(bed)?|gem\s*mint|gem\s*mt|gem|pristine|black label|pop\s*\d*)\b")
# Toy condition from the title. Only explicit wording counts; plain "new" is too common to mean sealed.
NEVER_OPENED = re.compile(r"\bnever (been )?opened\b")
SEALED = re.compile(r"\b(sealed|unopened|un opened|new in (bag|package|packaging|pack|box|plastic)|still in (bag|package|packaging|plastic)|"
                    r"in (original|orig) (bag|package|packaging|plastic)|nip|nib|mip|mib|nisp|nwt|new with tags?|factory sealed|never opened)\b")
OPENED = re.compile(r"\b(opened|loose|out of (the )?(bag|package|packaging|plastic)|no (bag|package|packaging|plastic|tags?)|"
                    r"unbagged|used|displayed|played with|removed from (the )?(bag|package|packaging))\b")


def condition(title):
    """'sealed', 'opened' or None (title doesn't say)."""
    t = norm(title)
    sealed = bool(SEALED.search(t))
    opened = bool(OPENED.search(NEVER_OPENED.sub(" ", t)))
    return "sealed" if sealed and not opened else "opened" if opened and not sealed else None


def grade_of(title):
    """'psa10', 'psa9' or 'other' for a graded card title, else None."""
    m = GRADE.search(norm(title))
    if not m:
        return None
    return {"psa10": "psa10", "psa9": "psa9"}.get(f"{m.group(1)}{m.group(2)}", "other")


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


def reject_reason(title, series, item):
    """None if the title is a single copy of this item, otherwise why it was rejected."""
    t = NEGATED.sub(" ", norm(title))
    # Titles naming only other years are older (or mislabelled) releases of a similar toy.
    years = set(YEAR.findall(t))
    if years and series.get("start", "")[:4] not in years:
        return f"different year: {', '.join(sorted(years))}"
    if not hits(t, series.get("require", [])):
        return "missing series words"
    if series["kind"] == "cards" and series.get("set_size"):
        for num, size in CARDNUM.findall(t):
            if int(size) != series["set_size"]:
                return f"different card set: /{size}"
            if str(item.get("number", "")).isdigit() and int(num) != int(item["number"]):
                return f"different card number: {num}/{size}"
    if not item_hits(t, item):
        return "not this item"
    m = (EXCLUDE_ANY.search(t) or (series["kind"] == "cards" and EXCLUDE_CARDS.search(t))
         or (series["kind"] == "toys" and (EXCLUDE_TOYS.search(t) or CARDNUM.search(t))))
    if m:
        return f"excluded word: {m.group(0)}"
    for w in series.get("exclude", []):
        if has(t, w):
            return f"series exclude: {w}"
    # A title that also fully matches another item is a multi-item listing.
    other = next((o for o in series["items"] if o is not item and item_hits(t, o)), None)
    return f"also names {other['name']}" if other else None


def graded_reason(title, series, item):
    """None if the title is one graded copy of this card, otherwise why it was rejected."""
    if not grade_of(title):
        return "not graded"
    if len(GRADE.findall(norm(title))) > 1:
        return "several grades"
    return reject_reason(GRADE_WORDS.sub(" ", norm(title)), series, item)


def matches(title, series, item):
    return reject_reason(title, series, item) is None


# Optional match report (set MATCH_REPORT=path): every kept and rejected title per item, for auditing.
REPORT = {}


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
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        # eBay explains auth failures in the body (e.g. invalid_client); it never contains the keys.
        body = e.read().decode("utf-8", "replace")[:300]
        hint = (" Check EBAY_CLIENT_ID is the App ID and EBAY_CLIENT_SECRET is the Cert ID (Production, no spaces),"
                " and that the keyset is enabled in the eBay developer portal.") if e.code in (400, 401) else ""
        raise RuntimeError(f"eBay sign-in failed: HTTP {e.code} {body}.{hint} {key_shape(client_id, secret)}") from None


def key_shape(client_id, secret):
    """Safe diagnostics about the keys' format (lengths and prefixes only, never the values)."""
    def ws(v):
        return "has whitespace" if any(c.isspace() for c in v) else "no whitespace"
    return (f"[App ID: {len(client_id)} chars, '-PRD-' inside: {'-PRD-' in client_id}, {ws(client_id)}; "
            f"Cert ID: {len(secret)} chars, starts 'PRD-': {secret.startswith('PRD-')}, {ws(secret)}; "
            f"same value twice: {client_id == secret}]")


def search(tok, q, category=None, attempts=4, us_sellers=False):
    params = {
        "q": q,
        "filter": "buyingOptions:{FIXED_PRICE},priceCurrency:USD,deliveryCountry:US" + (",itemLocationCountry:US" if us_sellers else ""),
        "limit": "200",
    }
    if category:
        params["category_ids"] = category
    req = urllib.request.Request(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}", headers={
        "Authorization": f"Bearer {tok}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        # Ship-to location, so listings that charge by distance return a real shipping cost.
        "X-EBAY-C-ENDUSERCTX": "contextualLocation=" + urllib.parse.quote(f"country=US,zip={SHIP_TO_ZIP}"),
    })
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r).get("itemSummaries", [])
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429 or i == attempts - 1:
                raise
        time.sleep(2 ** i * 3)


SHIP_TO_ZIP = "66044"  # a central US ZIP (Kansas): a fair middle for distance-based shipping


def watchers(it):
    try:
        return int(it["watchCount"])
    except (KeyError, TypeError, ValueError):
        return None


def item_price(it):
    try:
        return float(it["price"]["value"])
    except (KeyError, TypeError, ValueError):
        return None


def ship_cost(it):
    """Cheapest US shipping cost for a listing (0 for free shipping), or None when eBay doesn't say."""
    costs = []
    for o in it.get("shippingOptions") or []:
        if o.get("shippingCostType") == "FREE":
            costs.append(0.0)
        try:
            costs.append(float(o["shippingCost"]["value"]))
        except (KeyError, TypeError, ValueError):
            pass
    return min(costs) if costs else None


def delivered(matched):
    """[(listing, item_price, shipping, total)] where total = item + shipping to the US.
    Listings whose shipping eBay doesn't report get the typical (median) shipping of the others."""
    rows = [(it, item_price(it), ship_cost(it)) for it in matched]
    rows = [r for r in rows if r[1] is not None]
    known = [sh for _, _, sh in rows if sh is not None]
    typical = statistics.median(known) if known else 0.0
    return [(it, p, sh if sh is not None else typical, round(p + (sh if sh is not None else typical), 2), sh is not None)
            for it, p, sh in rows]


def stats(prices):
    prices = sorted(prices)
    q = statistics.quantiles(prices, n=4) if len(prices) >= 4 else [prices[0]] * 3
    return {
        "median": round(statistics.median(prices), 2),
        "p25": round(q[0], 2),
        "low": round(prices[0], 2),
        "count": len(prices),
    }


def sub_stats(rows, key_fn):
    """{group: {median, low, count}} of delivered prices, split by key_fn(listing) (None = left out)."""
    groups = {}
    for it, _p, _sh, total, _k in rows:
        k = key_fn(it.get("title", ""))
        if k:
            groups.setdefault(k, []).append(total)
    return {k: {"median": round(statistics.median(v), 2), "low": round(min(v), 2), "count": len(v)} for k, v in groups.items()}


def graded_point(tok, series, item, iid):
    """Graded-card prices for one card: PSA 10, PSA 9 and other grades, from one extra search."""
    q = f"{item['q']} (psa,cgc,bgs)"
    items = search(tok, q, series.get("category"))
    kept, rejected, seen_ids = [], [], set()
    for it in items:
        if it.get("itemId") in seen_ids:
            continue
        why = graded_reason(it.get("title", ""), series, item)
        if why is None:
            seen_ids.add(it.get("itemId")); kept.append(it)
        else:
            rejected.append({"title": it.get("title", ""), "price": (it.get("price") or {}).get("value"), "why": why})
    rows = delivered(kept)
    if os.environ.get("MATCH_REPORT"):
        REPORT[f"{iid}-GRADED"] = {"name": f"{item['name']} graded", "query": q,
                                    "kept": [{"title": it.get("title", ""), "grade": grade_of(it.get("title", "")), "total": t}
                                             for it, _p, _sh, t, _k in rows], "rejected": rejected}
    return sub_stats(rows, grade_of) or None


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


def deals(rows, median, limit=5):
    """Today's cheapest listings whose delivered price (item + shipping) is well under the median."""
    out = []
    for it, price, ship, total, _ in rows:
        if total <= median * DEAL_CUTOFF and it.get("itemWebUrl"):
            img = (it.get("image") or {}).get("imageUrl")
            out.append({"title": it.get("title", "")[:120], "price": total, "item": round(price, 2), "ship": round(ship, 2),
                        "url": it["itemWebUrl"], "below": round(1 - total / median, 3),
                        **({"image": img} if img else {})})
    return sorted(out, key=lambda d: d["price"])[:limit]


_TOKEN = None


def get_token():
    """One OAuth token per run (valid ~2 hours), instead of one per series."""
    global _TOKEN
    if _TOKEN is None:
        # eBay's sign-in occasionally fails once and then works; retry a few times before giving up.
        for attempt in range(4):
            try:
                _TOKEN = token(os.environ["EBAY_CLIENT_ID"].strip(), os.environ["EBAY_CLIENT_SECRET"].strip())
                break
            except RuntimeError:
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))
    return _TOKEN


def snapshot(series, today):
    """Return {item_id: (item_meta, point_or_None)} for one configured series."""
    tok = get_token()
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
        seen, matched, rejected = set(), [], []
        for it in items:
            if it.get("itemId") in seen:
                continue
            why = reject_reason(it.get("title", ""), series, item)
            if why is None:
                seen.add(it.get("itemId"))
                matched.append(it)
            else:
                rejected.append({"title": it.get("title", ""), "price": (it.get("price") or {}).get("value"), "why": why})
        rows = delivered(matched)  # prices are item + shipping to the US
        led = seen_for(series["id"])
        led["raw"].update(it.get("itemId") for it in items)
        led["rows"] += [(iid, *r[:4]) for r in rows]
        if os.environ.get("MATCH_REPORT"):
            REPORT[iid] = {"name": item["name"], "query": item["q"],
                           "kept": [{"title": it.get("title", ""), "price": p, "ship": sh, "total": t, "shipKnown": k}
                                    for it, p, sh, t, k in rows],
                           "rejected": rejected}
        prices = [t for *_, t, _k in rows]
        meta.update(best_image(matched, item))
        point = None
        if prices:
            point = {"date": today, **stats(prices),
                     "itemMedian": round(statistics.median([p for _, p, *_ in rows]), 2),
                     "shipMedian": round(statistics.median([sh for _, _, sh, *_ in rows]), 2)}
            # Demand: eBay's watcher counts on the matched listings (not every listing reports one).
            watch = [watchers(it) for it, *_ in rows if watchers(it) is not None]
            if watch:
                point["watchers"], point["watched"] = sum(watch), len(watch)
            # Toys: listings that say they're sealed vs all the others (few titles say "opened").
            if series["kind"] == "toys":
                cond = sub_stats(rows, lambda t: "sealed" if condition(t) == "sealed" else "other")
                if cond:
                    point["cond"] = cond
        # Cards: graded copies (PSA 10, PSA 9, other grades) from one extra search.
        if series["kind"] == "cards" and series.get("graded"):
            try:
                g = graded_point(tok, series, item, iid)
                if g and point:  # graded prices ride along on the raw card's daily point
                    point["graded"] = g
            except Exception as e:  # noqa: BLE001
                print(f"  ! {iid} graded: {e}")
        meta["deals"] = deals(rows, point["median"]) if point else []
        print(f"  {iid} {item['name']}: {len(prices)}/{len(items)} listings kept"
              + (f", median ${point['median']} incl. shipping (item ${point['itemMedian']} + ship ${point['shipMedian']})" if point else ""))
        out[iid] = (meta, point)
        time.sleep(0.5)
    return out


# ---------- Full-set listings ("Complete set of 15") ----------
SET_MARK = re.compile(r"\b((complete|full)\s+(\S+\s+){0,3}?(set|collection)|set of \d+|all \d+|set\s*#?1\s*(-|–|to)\s*\d+)\b")
SET_COUNT = re.compile(r"\b(?:set of|all|lot of|s/)\s*\(?(\d+)\)?|\b(\d+)\s*(?:pc|pcs|piece|pieces|toys?|cards?|figures?|buckets?|pails?)\b"
                       r"|#?\b1\s*(?:-|–|through|thru|to)\s*#?(\d+)\b")
SET_MULTI = re.compile(r"\b(\d+|two|three|four|five)\s+(complete\s+|full\s+)?sets\b|\b(2|3)x\b")
SET_REJECT = re.compile(
    r"\b(singles?|or set|or full|or complete|pick|choose|select|your choice|u pick|menu|variation|"
    r"incomplete|partial|missing|minus|except|doubles|duplicates|empty|box(es)? only|custom|fake|replica|parody|"
    r"aus|australia|australian|uk|canada|canadian|japan|japanese|malaysia|europe|nz|new zealand|mexico|asia|korea|philippines|"
    r"happy set|display|shelf|store|unused|poster sticker|sticker set|poster set|us limited|usa limited)\b")


def set_reason(title, series, size):
    """None if the title is one complete set of this series, otherwise why it was rejected."""
    t = NEGATED.sub(" ", norm(title))
    years = set(YEAR.findall(t))
    if years and series.get("start", "")[:4] not in years:
        return f"different year: {', '.join(sorted(years))}"
    if not hits(t, series.get("require", [])):
        return "missing series words"
    if not SET_MARK.search(t):
        return "not described as a full set"
    m = SET_REJECT.search(t)
    if m:
        return f"excluded word: {m.group(0)}"
    if SET_MULTI.search(t):
        return "more than one set"
    if len(re.findall(r"#\d+", t)) >= 2 and not re.search(r"#?\b1\s*(?:-|–|through|thru|to)\s*#?\d+", t):
        return "lists some item numbers (partial set)"
    counts = {int(a or b or c) for a, b, c in SET_COUNT.findall(t)}
    if counts and size not in counts:
        return f"wrong count: {sorted(counts)} (set has {size})"
    if series["kind"] == "toys" and EXCLUDE_TOYS.search(t):
        return "includes cards"
    if series["kind"] == "cards" and re.search(r"\b(toy|toys|figure|figures|charm|keychain)\b", t):
        return "includes toys"
    return None


def full_set(series, today):
    """Median delivered price of complete-set listings for one series (or None if none qualify)."""
    fs = series.get("full_set", {})
    size, q = fs.get("size", len(series["items"])), fs.get("q") or f"mcdonalds 2026 {series['name']} complete set"
    items = search(get_token(), q, series.get("category"), us_sellers=True)
    kept, rejected, seen = [], [], set()
    for it in items:
        if it.get("itemId") in seen:
            continue
        why = set_reason(it.get("title", ""), series, size)
        if why is None:
            seen.add(it.get("itemId")); kept.append(it)
        else:
            rejected.append({"title": it.get("title", ""), "price": (it.get("price") or {}).get("value"), "why": why})
    rows = delivered(kept)
    led = seen_for(series["id"])
    led["raw"].update(it.get("itemId") for it in items)
    led["rows"] += [(f"{series['id']}-SET", *r[:4]) for r in rows]
    if os.environ.get("MATCH_REPORT"):
        REPORT[f"{series['id']}-SET"] = {"name": f"{series['name']} complete set", "query": q,
                                          "kept": [{"title": it.get("title", ""), "price": p, "ship": sh, "total": t, "shipKnown": k}
                                                   for it, p, sh, t, k in rows], "rejected": rejected}
    print(f"  {series['id']} full set: {len(rows)}/{len(items)} listings kept"
          + (f", median ${statistics.median([r[3] for r in rows]):.2f} incl. shipping" if rows else ""))
    if not rows:
        return None
    totals = [r[3] for r in rows]
    point = {"date": today, **stats(totals),
             "itemMedian": round(statistics.median([r[1] for r in rows]), 2),
             "shipMedian": round(statistics.median([r[2] for r in rows]), 2)}
    return {"point": point, "deals": deals(rows, point["median"], limit=3), "size": size,
            "search": "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode({"_nkw": q})}


def item_meta(series, item):
    return {"name": item["name"], "set": series["id"], "number": item["number"],
            "rarity": "Holo" if item.get("holo") else None}
