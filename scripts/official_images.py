"""Official toy photos from McDonald's Happy Meal site (happymeal.com), saved into the repo.

A series opts in with "official_images": "<site url>" in data/config.json. Photos come from two places:
  1. the site's toy lineup page (current series only), matched by image file name or alt text;
  2. the site's public WordPress media library (/wp-json/wp/v2/media), which still holds the photos
     of past promotions. Only uploads from about four months before the series starts until it
     ends are considered, because file names like Type1.png are reused by every promotion.
An item can name its photo explicitly with "official": "<file name or label>" when McDonald's calls
the toy something else (e.g. "Pearl x Nico" for our "Pearl x Robin").

Each photo is downloaded once to assets/toys/<item id>.<ext> and kept, so it keeps working after the
site drops it. Saved photos take priority over eBay listing photos. Items with no confident match keep
their eBay photo.

Standard library only.
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOY_DIR = ROOT / "assets" / "toys"
EXTS = (".png", ".webp", ".jpg", ".jpeg")
UA = {"User-Agent": "Mozilla/5.0 (compatible; toy-ticker/1.0)"}
CREDIT = "Official photo: happymeal.com"
LOOKBACK_DAYS = 120  # photos are often uploaded weeks before a promotion starts
# Uploads that are page furniture, not a single toy.
JUNK = re.compile(r"hover|logo|background|[-_ ]bg|bg[-_ ]|activit|graphic|sheet|legal|lock-?up|desktop|mobile|tablet"
                  r"|toy[-_ ]?s(e)?ction|toy[-_ ]?area|power[-_ ]?up|\bpu\b|^toys?$", re.I)
STOP = {"x", "the", "and", "with", "a", "an", "of", "on", "in", "s", "happy", "meal", "toy"}


def key(s):
    """Letters and digits only, lower case, '×'/' x ' joiners dropped: 'SpongeBob × Luffy' == 'spongebob x luffy'."""
    s = re.sub(r"\s+x\s+|×", " ", s, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tokens(s):
    return set(re.findall(r"[a-z0-9]+", s.lower().replace("×", " ")))


def stem(url):
    return Path(urllib.parse.urlparse(url).path).stem


class _Imgs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []  # (src, alt)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            a = dict(attrs)
            src = a.get("src") or a.get("data-src") or ""
            if src and not src.startswith("data:"):
                self.found.append((src, a.get("alt") or ""))


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def page_candidates(site):
    """Photos on the lineup page: [(url, labels, date)]."""
    p = _Imgs()
    p.feed(get(site).decode("utf-8", "replace"))
    out = []
    for src, alt in p.found:
        src = urllib.parse.urljoin(site, src)
        words = re.findall(r"[A-Za-z0-9]+", alt)
        # 'Pikachu shaped like a Happy Meal box' -> the name is the alt text's opening words
        out.append((src, [stem(src), alt] + [" ".join(words[:n]) for n in (1, 2, 3)], ""))
    return out


_MEDIA = {}


def media_candidates(site, after, before):
    """Uploads in the site's media library between two dates: [(url, labels, date)], newest first."""
    k = (site, after, before)
    if k in _MEDIA:
        return _MEDIA[k]
    api = urllib.parse.urljoin(site, "/wp-json/wp/v2/media")
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({"per_page": 100, "page": page, "media_type": "image", "orderby": "date",
                                    "after": f"{after}T00:00:00", "before": f"{before}T23:59:59",
                                    "_fields": "date,source_url,alt_text,title"})
        try:
            rows = json.loads(get(f"{api}?{q}"))
        except urllib.error.HTTPError as e:
            if e.code == 400 and page > 1:  # past the last page
                break
            raise
        for r in rows:
            src = r.get("source_url") or ""
            title = re.sub(r"<[^>]+>", "", (r.get("title") or {}).get("rendered", ""))
            out.append((src, [stem(src), title, r.get("alt_text") or ""], r.get("date", "")))
        if len(rows) < 100:
            break
        page += 1
    _MEDIA[k] = out
    return out


def pick(item, cands):
    """The one photo that shows this item, or None when there's no confident match."""
    ov = item.get("official")
    if ov:
        hits = [c for c in cands if c[0].endswith("/" + ov) or key(ov) in {key(l) for l in c[1]}]
        return hits[0][0] if hits else None
    usable = [c for c in cands if not any(JUNK.search(l) for l in c[1][:2] if l)]
    name = key(item["name"])
    exact = [c for c in usable if name in {key(l) for l in c[1] if l}]
    if exact:
        return exact[0][0]  # newest upload wins if a photo was re-uploaded
    want = tokens(item["name"]) - STOP
    if not want:
        return None
    hits = {}
    for c in usable:
        if want <= tokens(" ".join(c[1])):
            hits.setdefault(c[0], len(tokens(" ".join(c[1][1:]))))
    if not hits:
        return None
    ranked = sorted(hits.items(), key=lambda kv: kv[1])
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None  # two equally good photos: ambiguous
    return ranked[0][0]


def saved(iid):
    for ext in EXTS:
        f = TOY_DIR / f"{iid}{ext}"
        if f.exists():
            return f
    return None


def meta(path, site):
    return {"image": path.relative_to(ROOT).as_posix(), "imageLink": site, "imageCredit": CREDIT,
            "imageSource": "official"}


def window(series):
    start = date.fromisoformat(series["start"])
    end = date.fromisoformat(series["end"]) if series.get("end") else start + timedelta(days=60)
    return (start - timedelta(days=LOOKBACK_DAYS)).isoformat(), max(end, start).isoformat()


def collect(series):
    """{item id: image metadata} for every item of the series with an official photo (saved or new)."""
    site = series.get("official_images")
    if not site:
        return {}
    items = {f"{series['id']}-{it['number']}": it for it in series["items"]}
    out = {iid: meta(f, site) for iid in items if (f := saved(iid))}
    missing = {iid: it for iid, it in items.items() if iid not in out}
    if not missing:
        return out
    sources = []
    if series.get("current"):
        sources.append(("lineup page", lambda: page_candidates(site)))
    if series.get("start"):
        sources.append(("media library", lambda: media_candidates(site, *window(series))))
    found = {}
    for label, load in sources:
        if not (todo := [iid for iid in missing if iid not in found]):
            break
        try:
            cands = load()
        except Exception as e:  # noqa: BLE001  (site down or changed: keep what's saved)
            print(f"! {series['id']} official photos ({label}): {e}", file=sys.stderr)
            continue
        for iid in todo:
            if url := pick(missing[iid], cands):
                found[iid] = url
    if found:
        TOY_DIR.mkdir(parents=True, exist_ok=True)
    for iid, url in found.items():
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if ext not in EXTS:
            continue
        try:
            body = get(url)
        except Exception as e:  # noqa: BLE001
            print(f"! {iid} official photo: {e}", file=sys.stderr)
            continue
        f = TOY_DIR / f"{iid}{ext}"
        f.write_bytes(body)
        out[iid] = meta(f, site)
        print(f"  official photo for {missing[iid]['name']}: {stem(url)}{ext} -> {f.relative_to(ROOT)}")
    for iid in missing:
        if iid not in out:
            print(f"  no official photo for {missing[iid]['name']}", file=sys.stderr)
    return out
