"""Search pages: static HTML that crawlers and AI assistants can read without running JavaScript.

Writes:
  s/<series>/index.html  every item's price in plain HTML (series and groups), with ItemList JSON-LD
  i/<item>/index.html    one page per toy or card, with Product + AggregateOffer JSON-LD
  sitemap.xml            every page above plus the home and about pages
and refreshes the marked blocks in index.html (Dataset/WebSite JSON-LD, plain links to every
series page) and about.html (FAQPage JSON-LD built from its <details> questions).

The interactive app stays at ./?s=<series>; these pages link to it. Run on its own with
`python3 scripts/seo.py` to rebuild from the published data/ files (no eBay keys needed).
"""
import html
import json
import re
import shutil
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND = "Toy Ticker"
HISTORY_ROWS = 14
DEALS = 5

esc = html.escape


def money(v):
    return f"${v:,.2f}" if v is not None else "–"


def nice(d):
    return date.fromisoformat(d).strftime("%b %-d, %Y") if d else ""


def year(s):
    return (s.get("start") or "")[:4]


def ld(obj):
    """JSON-LD script tag; '<' is escaped so text can't close the tag."""
    return '<script type="application/ld+json">' + json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c") + "</script>"


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


# ---------- data ----------

def load_published(root=ROOT):
    """Rebuild the fetcher's in-memory shape (sets, cards, history, groups) from data/."""
    idx = json.loads((root / "data" / "index.json").read_text())
    cards = {iid: {k: v for k, v in it.items() if k != "latest"} for iid, it in idx["items"].items()}
    history = {}
    for f in (root / "data" / "series").glob("*.json"):
        history.update(json.loads(f.read_text())["history"])
    return {"sets": idx["sets"], "cards": cards, "history": history, "groups": idx.get("groups", []),
            "updated": idx.get("updated")}


def img_src(path, prefix):
    """Local images live under assets/; card photos are full eBay URLs."""
    return path if path.startswith(("http://", "https://")) else prefix + path


def item_noun(s):
    return "card" if s.get("kind") == "cards" else "toy"


def kind_words(s):
    return {"cards": "cards", "mixed": "toys and cards"}.get(s.get("kind"), "toys")


def full_name(c, s):
    """'Decidueye 001/015' for cards (the number collectors search for), plain name for toys."""
    if s.get("kind") == "cards" and c.get("number"):
        size = (s.get("fullSet") or {}).get("size")
        return f"{c['name']} {c['number']}/{size:03d}" if size else f"{c['name']} #{c['number']}"
    return c["name"]


class Site:
    def __init__(self, data, site_url):
        self.d, self.url = data, site_url
        self.sets = data["sets"]
        self.order = sorted(self.sets, key=lambda k: self.sets[k].get("order", 0))
        self.group_of = {m: g for g in data.get("groups", []) for m in g["members"]}
        self.items = {sid: [iid for iid, c in data["cards"].items() if c.get("set") == sid] for sid in self.sets}

    def last(self, iid):
        h = self.d["history"].get(iid) or []
        return h[-1] if h else None

    def series_url(self, sid):
        return f"{self.url}s/{sid}/"

    def item_url(self, iid):
        return f"{self.url}i/{iid}/"

    def group(self, g):
        ms = [self.sets[m] for m in g["members"]]
        return {**g, "kind": "mixed", "start": min(m["start"] for m in ms),
                "end": max((m.get("end") or "") for m in ms) or None, "current": any(m.get("current") for m in ms)}

    def units(self):
        """Series as the site shows them (groups replace their members), plus the members themselves."""
        shown, done = [], set()
        for sid in self.order:
            g = self.group_of.get(sid)
            if not g:
                shown.append((sid, self.sets[sid], [sid]))
            elif g["id"] not in done:
                done.add(g["id"])
                shown.append((g["id"], self.group(g), g["members"]))
        members = [(sid, self.sets[sid], [sid]) for sid in self.order if sid in self.group_of]
        return shown, members


# ---------- page shell ----------

CSS = """
:root{--bg:#fafaf7;--panel:#fff;--text:#1d1d1b;--muted:#6b6b66;--line:#e6e5df;--accent:#d62300;--soft:#fde9e4;--up:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1e1e1c;--text:#ecebe6;--muted:#9b9a94;--line:#2f2f2c;--accent:#ff6a4d;--soft:#3a211b;--up:#4cc26a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:24px 16px 64px}
h1,h2,.brand,.big{font-family:"Fredoka",-apple-system,sans-serif}
a{color:var(--accent)}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:22px;color:var(--text);text-decoration:none}
.brand img{width:34px;height:34px}.brand span{color:var(--accent)}
.crumbs{font-size:13px;color:var(--muted);margin:0 0 6px}.crumbs a{color:var(--muted)}
h1{font-size:clamp(26px,5vw,36px);line-height:1.15;margin:0 0 10px;letter-spacing:-.01em}
h2{font-size:22px;margin:34px 0 10px}
.answer{font-size:18px;margin:0 0 14px}
.muted{color:var(--muted);font-size:14px}
.pill{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:3px 10px;border-radius:99px;background:var(--soft);color:var(--accent)}
.cta{display:inline-block;margin:14px 0 4px;padding:10px 18px;border-radius:12px;background:var(--accent);color:#fff;font-weight:600;text-decoration:none}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}
.stats div{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.stats .big{display:block;font-size:26px;font-weight:700}.stats small{color:var(--muted);font-size:13px}
.list{list-style:none;padding:0;margin:0;border:1px solid var(--line);border-radius:14px;overflow:hidden;background:var(--panel)}
.list li{display:grid;grid-template-columns:52px 1fr auto;gap:12px;align-items:center;padding:8px 14px;border-top:1px solid var(--line)}
.list li:first-child{border-top:0}
.list img{width:52px;height:52px;object-fit:contain}
.list .nm{font-weight:600}.list .sub{display:block;color:var(--muted);font-size:13px;font-weight:400}
.list .pr{text-align:right;font-weight:700;font-variant-numeric:tabular-nums}
.hero{display:grid;grid-template-columns:minmax(0,220px) 1fr;gap:22px;align-items:start}
.hero img{width:100%;height:auto;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:10px}
@media (max-width:560px){.hero{grid-template-columns:1fr}.hero img{max-width:220px}}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:15px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600;font-size:13px}
td.n,th.n{text-align:right}
.tbl{overflow-x:auto}
footer{margin-top:44px;color:var(--muted);font-size:12px}
"""

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{url}">
<link rel="icon" href="../../assets/logo.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@600;700&display=swap" rel="stylesheet">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Toy Ticker">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{image}">
<meta property="og:url" content="{url}">
<meta name="twitter:card" content="summary_large_image">
<style>{css}</style>
{jsonld}
</head><body>
<div class="wrap">
<div class="top"><a class="brand" href="../../"><img src="../../assets/logo.svg" alt="" width="34" height="34">Toy <span>Ticker</span></a>
<a href="../../about.html">How prices work</a></div>
{body}
<footer>
<p>Prices are eBay US Buy It Now <b>asking</b> prices including shipping, collected daily with the eBay Browse API. Lots, sets and graded copies are filtered out. Not financial advice. <a href="../../about.html">Methodology</a></p>
<p>Unofficial fan site. Not affiliated with or endorsed by McDonald’s, eBay or any toy brand. All trademarks belong to their owners.</p>
</footer>
</div>
</body></html>
"""


def page(title, desc, url, image, body, jsonld):
    return PAGE.format(title=esc(title), desc=esc(desc), url=esc(url), image=esc(image), css=CSS.strip(),
                       jsonld="\n".join(ld(j) for j in jsonld), body=body)


def crumbs(site, trail):
    """trail: [(name, url)]; the last entry is the current page."""
    links = " › ".join(f'<a href="{esc(u)}">{esc(n)}</a>' for n, u in trail[:-1]) + f" › {esc(trail[-1][0])}"
    data = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, "item": u} for i, (n, u) in enumerate(trail)]}
    return f'<nav class="crumbs" aria-label="Breadcrumb">{links}</nav>', data


def status(s, today):
    if s.get("current") or (s.get("end") and s["start"] <= today <= s["end"]):
        return "In restaurants now"
    if s.get("end"):
        return f"Ended {nice(s['end'])}"
    return "Past series"


# ---------- series pages ----------

def item_row(site, iid, s):
    c, p = site.d["cards"][iid], site.last(iid)
    alt = f"McDonald's {year(s)} {s['name']} Happy Meal {item_noun(s)}: {c['name']}"
    img = f'<img src="{esc(img_src(c["image"], "../../"))}" alt="{esc(alt)}" width="52" height="52" loading="lazy">' if c.get("image") else "<span></span>"
    sub = f"{p['count']} listings · low {money(p['low'])}" if p else "No single listings yet"
    return (f'<li>{img}<span><a class="nm" href="../../i/{esc(iid)}/">{esc(full_name(c, s))}</a>'
            f'<span class="sub">{esc(sub)}</span></span><span class="pr">{money(p["median"]) if p else "–"}</span></li>')


def series_page(site, uid, s, members, today, parent=None):
    rows = [(m, iid) for m in members for iid in site.items[m]]
    priced = [(iid, site.last(iid)) for _, iid in rows if site.last(iid)]
    dates = sorted({p["date"] for _, p in priced})
    asof = dates[-1] if dates else None
    total = sum(p["median"] for _, p in priced)
    listings = sum(p["count"] for _, p in priced)
    yr = year(s)
    kw = kind_words(s)
    top = max(priced, key=lambda x: x[1]["median"]) if priced else None
    low = min(priced, key=lambda x: x[1]["median"]) if priced else None
    name_of = lambda iid: site.d["cards"][iid]["name"]  # noqa: E731

    title = f"McDonald's {s['name']} {yr}: Happy Meal prices · {BRAND}"
    if priced:
        desc = (f"All {len(rows)} McDonald's {s['name']} ({yr}) Happy Meal {kw}: full set {money(total)} on {nice(asof)}. "
                f"Most valuable: {name_of(top[0])} at {money(top[1]['median'])}. eBay prices, updated daily.")
        answer = (f"As of {nice(asof)}, a complete set of all {len(rows)} {yr} McDonald's {esc(s['name'])} Happy Meal {kw} "
                  f"costs about <b>{money(total)}</b> at median eBay asking prices, shipping included"
                  + (f" ({len(priced)} of {len(rows)} items priced)" if len(priced) < len(rows) else "")
                  + f". The most valuable is <b>{esc(name_of(top[0]))}</b> at {money(top[1]['median'])}; "
                  f"the cheapest is {esc(name_of(low[0]))} at {money(low[1]['median'])}.")
    else:
        desc = f"Daily eBay prices for all {len(rows)} McDonald's {s['name']} ({yr}) Happy Meal {kw}."
        answer = f"Prices for the {yr} McDonald's {esc(s['name'])} Happy Meal {kw} appear after the first daily eBay update."

    trail = [(BRAND, site.url)] + ([(parent[1]["name"], site.series_url(parent[0]))] if parent else []) + [(s["name"], site.series_url(uid))]
    nav, bc = crumbs(site, trail)
    run = f"{nice(s['start'])} – {nice(s['end']) if s.get('end') else 'now'}"
    body = [nav, f'<span class="pill">{esc(status(s, today))}</span>',
            f"<h1>McDonald's {esc(s['name'])} ({yr}) prices</h1>",
            f'<p class="answer">{answer}</p>']
    if s.get("description"):
        body.append(f"<p>{esc(s['description'])}</p>")
    body.append(f'<a class="cta" href="../../?s={esc(uid)}">Open the live tracker with charts →</a>')
    body.append('<div class="stats">'
                f'<div><span class="big">{money(total) if priced else "–"}</span><small>Full set, median incl. shipping</small></div>'
                f'<div><span class="big">{len(rows)}</span><small>{esc(kw)} in the series</small></div>'
                f'<div><span class="big">{listings:,}</span><small>eBay listings checked</small></div>'
                f'<div><span class="big">{esc(nice(asof) or "–")}</span><small>Last price update</small></div></div>')
    body.append(f'<p class="muted">In McDonald\'s restaurants {esc(run)}.</p>')
    for m in members:
        ms = site.sets[m]
        heading = f"{ms['name']}" if len(members) > 1 else f"Every {item_noun(s)} by price"
        more = f' <a class="muted" href="../{esc(m)}/">(page)</a>' if len(members) > 1 else ""
        ids = sorted(site.items[m], key=lambda i: -(site.last(i) or {}).get("median", -1))
        body.append(f"<h2>{esc(heading)}{more}</h2>")
        body.append('<ul class="list">' + "".join(item_row(site, iid, ms) for iid in ids) + "</ul>")
    if parent:
        body.append(f'<p class="muted">Part of the <a href="../{esc(parent[0])}/">{esc(parent[1]["name"])}</a> Happy Meal.</p>')

    image = f"{site.url}og/{uid}.png"
    items_ld = {"@context": "https://schema.org", "@type": "CollectionPage", "name": title, "url": site.series_url(uid),
                "description": desc, "dateModified": asof or today, "isPartOf": {"@type": "WebSite", "name": BRAND, "url": site.url},
                "mainEntity": {"@type": "ItemList", "numberOfItems": len(rows), "itemListElement": [
                    {"@type": "ListItem", "position": i + 1, "url": site.item_url(iid), "name": full_name(site.d["cards"][iid], site.sets[m])}
                    for i, (m, iid) in enumerate(rows)]}}
    return page(title, desc, site.series_url(uid), image, "\n".join(body), [items_ld, bc]), asof


# ---------- item pages ----------

def item_page(site, iid, today):
    c = site.d["cards"][iid]
    sid = c["set"]
    s = site.sets[sid]
    g = site.group_of.get(sid)
    hist = site.d["history"].get(iid) or []
    p = hist[-1] if hist else None
    yr, noun, fname = year(s), item_noun(s), full_name(c, s)
    label = f"{fname} {noun}" if noun == "card" else fname

    title = f"{label} value: McDonald's {s['name']} {yr} · {BRAND}"
    if p:
        desc = (f"{fname} from the {yr} McDonald's {s['name']} Happy Meal is worth about {money(p['median'])} on eBay "
                f"(median incl. shipping, {nice(p['date'])}). Lowest {money(p['low'])}, {p['count']} listings. Updated daily.")
        answer = (f"As of {nice(p['date'])}, the median eBay asking price for the McDonald's <b>{esc(label)}</b> "
                  f"({esc(s['name'])}, {yr}) is <b>{money(p['median'])}</b> including shipping, across {p['count']} active "
                  f"Buy It Now listings. The cheapest is {money(p['low'])}.")
    else:
        desc = f"Daily eBay prices for {fname} from the {yr} McDonald's {s['name']} Happy Meal."
        answer = (f"There are no single, ungraded {esc(label)} listings on eBay right now, so there's no fair price yet. "
                  "It appears with the next daily update that finds one.")

    trail = [(BRAND, site.url)] + ([(g["name"], site.series_url(g["id"]))] if g else []) + [(s["name"], site.series_url(sid)), (fname, site.item_url(iid))]
    nav, bc = crumbs(site, trail)
    alt = f"McDonald's {yr} {s['name']} Happy Meal {noun}: {c['name']}"
    img = (f'<figure style="margin:0"><img src="{esc(img_src(c["image"], "../../"))}" alt="{esc(alt)}" width="220" height="220">'
           + (f'<figcaption class="muted">{esc(c["imageCredit"])}</figcaption>' if c.get("imageCredit") else "")
           + "</figure>") if c.get("image") else "<div></div>"
    stats = ""
    if p:
        stats = ('<div class="stats">'
                 f'<div><span class="big">{money(p["median"])}</span><small>Median price incl. shipping</small></div>'
                 f'<div><span class="big">{money(p["low"])}</span><small>Lowest listing today</small></div>'
                 f'<div><span class="big">{money(p.get("p25"))}</span><small>25th percentile</small></div>'
                 f'<div><span class="big">{p["count"]}</span><small>Active listings</small></div></div>')
    body = [nav, f'<div class="hero">{img}<div>',
            f"<h1>{esc(label)} price: McDonald's {esc(s['name'])} ({yr})</h1>",
            f'<p class="answer">{answer}</p>', stats,
            f'<a class="cta" href="../../?s={esc(g["id"] if g else sid)}">See the price chart →</a></div></div>']

    cond = (p or {}).get("cond") or {}
    if len(cond) > 1:
        names = {"sealed": "Sealed / new in bag", "other": "Opened or unspecified", "graded": "Graded"}
        body.append("<h2>Price by condition</h2><div class=\"tbl\"><table><tr><th>Condition</th><th class=n>Median</th><th class=n>Lowest</th><th class=n>Listings</th></tr>"
                    + "".join(f"<tr><td>{esc(names.get(k, k.title()))}</td><td class=n>{money(v.get('median'))}</td><td class=n>{money(v.get('low'))}</td><td class=n>{v.get('count', '')}</td></tr>"
                              for k, v in sorted(cond.items(), key=lambda kv: -kv[1].get("count", 0)))
                    + "</table></div>")
    if hist:
        body.append(f"<h2>Price history</h2><div class=\"tbl\"><table><tr><th>Date</th><th class=n>Median</th><th class=n>Lowest</th><th class=n>Listings</th></tr>"
                    + "".join(f"<tr><td>{esc(nice(h['date']))}</td><td class=n>{money(h.get('median'))}</td><td class=n>{money(h.get('low'))}</td><td class=n>{h.get('count', '')}</td></tr>"
                              for h in reversed(hist[-HISTORY_ROWS:]))
                    + "</table></div>")
    deals = [x for x in (c.get("deals") or []) if x.get("url") and x.get("price") is not None][:DEALS]
    if deals:
        body.append("<h2>Cheapest listings right now</h2><ul>"
                    + "".join(f'<li><a href="{esc(x["url"])}" rel="nofollow noopener" target="_blank">{esc(x.get("title") or "eBay listing")}</a> – {money(x["price"])}</li>' for x in deals)
                    + "</ul>")
    siblings = [i for i in site.items[sid] if i != iid]
    if siblings:
        body.append(f"<h2>More from {esc(s['name'])}</h2><p>"
                    + " · ".join(f'<a href="../{esc(i)}/">{esc(site.d["cards"][i]["name"])}</a>' for i in siblings) + "</p>")

    image = img_src(c["image"], site.url) if c.get("image") else f"{site.url}og/{sid}.png"
    product = {"@context": "https://schema.org", "@type": "Product", "name": f"McDonald's {s['name']} {yr} – {fname}",
               "description": desc, "image": image, "url": site.item_url(iid), "category": f"Happy Meal {noun}s",
               "brand": {"@type": "Brand", "name": "McDonald's"}}
    if c.get("number"):
        product["sku"] = str(c["number"])
    if p:
        product["offers"] = {"@type": "AggregateOffer", "priceCurrency": s.get("currency", "USD"), "lowPrice": round(p["low"], 2),
                             "offerCount": p["count"], "availability": "https://schema.org/InStock"}
    return page(title, desc, site.item_url(iid), image, "\n".join(body), [product, bc]), (p or {}).get("date")


# ---------- home/about blocks, sitemap ----------

def replace_block(path, name, content):
    """Swap the text between <!-- seo:NAME --> and <!-- /seo:NAME --> (left alone if the markers are missing)."""
    text = path.read_text()
    pat = re.compile(rf"(<!-- seo:{name} -->).*?(<!-- /seo:{name} -->)", re.S)
    if not pat.search(text):
        return False
    new = pat.sub(lambda m: m.group(1) + "\n" + content + "\n" + m.group(2), text)
    if new != text:
        path.write_text(new)
    return True


def home_blocks(site, root, shown, updated):
    starts = [s["start"] for s in site.sets.values() if s.get("start")]
    dataset = {"@context": "https://schema.org", "@type": "Dataset",
               "name": "Toy Ticker: daily eBay prices for McDonald's Happy Meal toys and cards",
               "description": ("Daily median, 25th-percentile and lowest eBay US Buy It Now asking prices (shipping included) "
                               "and listing counts for every toy and card in recent McDonald's Happy Meal series, "
                               f"{len(site.d['cards'])} items across {len(site.sets)} series."),
               "url": site.url, "isAccessibleForFree": True, "dateModified": (updated or "")[:10] or None,
               "temporalCoverage": f"{min(starts)}/.." if starts else None,
               "keywords": ["Happy Meal toys", "McDonald's toys", "toy prices", "eBay prices", "collectibles"],
               "creator": {"@type": "Organization", "name": BRAND, "url": site.url},
               "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": f"{site.url}data/index.json"}]}
    dataset = {k: v for k, v in dataset.items() if v is not None}
    website = {"@context": "https://schema.org", "@type": "WebSite", "name": BRAND, "url": site.url,
               "description": "Daily eBay prices for McDonald's Happy Meal toys and cards."}
    replace_block(root / "index.html", "jsonld", ld(website) + "\n" + ld(dataset))
    links = " · ".join(f'<a href="s/{esc(uid)}/">{esc(s["name"])} ({year(s)})</a>' for uid, s, _ in shown)
    replace_block(root / "index.html", "links", f'<p class="disclaimer">Price lists: {links}</p>')


def about_faq(root):
    path = root / "about.html"
    text = path.read_text()
    qa = re.findall(r"<details><summary>(.*?)</summary>\s*<p>(.*?)</p></details>", text, re.S)
    if qa:
        replace_block(path, "faq", ld({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": strip_tags(q), "acceptedAnswer": {"@type": "Answer", "text": strip_tags(a)}} for q, a in qa]}))


def sitemap(entries):
    rows = "".join(f"<url><loc>{esc(u)}</loc>" + (f"<lastmod>{d}</lastmod>" if d else "") + "</url>\n" for u, d in entries)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{rows}</urlset>\n'


def build(data, root, site_url):
    root = Path(root)
    site = Site(data, site_url)
    today = date.today().isoformat()
    shown, members = site.units()
    updated = data.get("updated")
    entries = [(site_url, (updated or today)[:10]), (f"{site_url}about.html", None)]

    for uid, s, ms in shown + members:
        parent = None
        if uid in site.group_of:
            g = site.group_of[uid]
            parent = (g["id"], g)
        text, asof = series_page(site, uid, s, ms, today, parent)
        out = root / "s" / uid / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        entries.append((site.series_url(uid), asof))

    keep = set()
    for iid in data["cards"]:
        text, asof = item_page(site, iid, today)
        out = root / "i" / iid / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        keep.add(iid)
        entries.append((site.item_url(iid), asof))
    for d in (root / "i").iterdir():  # items no longer tracked
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d)

    home_blocks(site, root, shown, updated)
    about_faq(root)
    (root / "sitemap.xml").write_text(sitemap(entries))
    return len(shown) + len(members), len(keep)


if __name__ == "__main__":
    cfg = json.loads((ROOT / "data" / "config.json").read_text())
    n_series, n_items = build(load_published(ROOT), ROOT, cfg.get("site_url", ""))
    print(f"Search pages: {n_series} series, {n_items} items, sitemap.xml")
