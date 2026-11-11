"""Link-preview images (Open Graph, 1200x630 PNG) and share pages for each series.

Writes og/<series>.png, og/site.png and s/<series>/index.html. The share pages carry
the series' preview tags (crawlers don't run JavaScript, so ?s=... links can't) and
send people on to the normal page. Needs Pillow.
"""
import html
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
THEMES = {  # gradient start, gradient end, text colour (keep in sync with index.html)
    "pokemon-2026": ("#ffcb05", "#f08a00", "#1d1d1b"),
    "boo-buckets-2026": ("#ff7a1a", "#5b1f8f", "#ffffff"),
    "spongebob-onepiece-2026": ("#0aa5d8", "#0b4f8a", "#ffffff"),
}
DEFAULT = ("#d62300", "#ffb703", "#ffffff")


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class Fonts:
    def __init__(self, path):
        self.path = path
        self.cache = {}

    def __call__(self, size, weight=600):
        key = (size, weight)
        if key not in self.cache:
            f = ImageFont.truetype(str(self.path), size)
            try:
                axes = [a["name"] for a in f.get_variation_axes()]
                f.set_variation_by_axes([weight if n == b"Weight" else 100 for n in axes])
            except Exception:  # noqa: BLE001 - static font fallback
                pass
            self.cache[key] = f
        return self.cache[key]


def gradient(c1, c2):
    a, b = rgb(c1), rgb(c2)
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(0, W, 2):
            t = min(1, (x / W * 0.6 + y / H * 0.4) * 1.05)
            c = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
            px[x, y] = c
            if x + 1 < W:
                px[x + 1, y] = c
    return img


def dots(img, ink):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    col = rgb(ink)
    for y in range(12, H, 26):
        for x in range(W // 2, W, 26):
            alpha = int(40 * (x - W / 2) / (W / 2))
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(*col, alpha))
    img.paste(layer, (0, 0), layer)


def logo(d, x, y, s=1.0):
    """The site's logo (assets/logo.svg: smiling box on a yellow tile), drawn with shapes
    because Pillow can't render SVG. Coordinates are in the SVG's 64x64 space."""
    k = lambda v: v * s  # noqa: E731
    d.rounded_rectangle([x + k(4), y + k(4), x + k(60), y + k(60)], radius=k(16), fill="#f2b705")
    d.arc([x + k(25), y + k(14), x + k(39), y + k(30)], 180, 360, fill="#d62300", width=max(1, int(k(4))))
    d.polygon([(x + k(15), y + k(24)), (x + k(49), y + k(24)), (x + k(46), y + k(50.6)), (x + k(18), y + k(50.6))], fill="#d62300")
    for cx in (26, 38):
        d.ellipse([x + k(cx - 2.6), y + k(31.4), x + k(cx + 2.6), y + k(36.6)], fill="#ffffff")
    d.arc([x + k(24), y + k(35), x + k(40), y + k(45)], 20, 160, fill="#f2b705", width=max(1, int(k(3.5))))


def pill(d, x, y, text, font, ink):
    tw = d.textlength(text, font=font)
    d.rounded_rectangle([x, y, x + tw + 36, y + 44], radius=22, fill=(255, 255, 255, 60) if ink == "#ffffff" else (0, 0, 0, 28))
    d.text((x + 18, y + 22), text, font=font, fill=ink, anchor="lm")
    return x + tw + 36


def fit(d, text, font_fn, max_w, start, weight=700, min_size=40):
    size = start
    while size > min_size and d.textlength(text, font=font_fn(size, weight)) > max_w:
        size -= 4
    return font_fn(size, weight)


def series_rows(data, sid):
    rows = []
    for iid, c in data["cards"].items():
        if c.get("set") != sid:
            continue
        h = data["history"].get(iid) or []
        rows.append((c["name"], h[-1]["median"] if h else None))
    return rows


def money(v):
    return f"${v:,.2f}"


def series_image(s, sid, rows, today, F, site_label):
    c1, c2, ink = THEMES.get(sid, DEFAULT)
    img = gradient(c1, c2)
    dots(img, ink)
    d = ImageDraw.Draw(img, "RGBA")

    logo(d, 60, 44, 1.0)
    d.text((136, 76), site_label, font=F(30, 600), fill=ink, anchor="lm")

    live = s.get("current") or (s.get("end") and s["start"] <= today <= s["end"])
    status = "NOW IN RESTAURANTS" if live else (f"ENDED {date.fromisoformat(s['end']).strftime('%b %-d, %Y').upper()}" if s.get("end") else "PAST SERIES")
    pill(d, 60, 150, status, F(24, 700), ink)

    title = s["name"]
    d.text((60, 214), title, font=fit(d, title, F, 700, 76), fill=ink)
    kind = "cards" if s.get("kind") == "cards" else "toys"
    d.text((62, 316), f"{len(rows)} {kind} · daily eBay prices", font=F(30, 500), fill=ink)

    priced = sorted([r for r in rows if r[1] is not None], key=lambda r: -r[1])
    # Right column: full-set value.
    rx = W - 60
    if priced:
        d.text((rx, 170), "FULL SET", font=F(24, 600), fill=ink, anchor="ra")
        d.text((rx, 200), money(sum(p for _, p in priced)), font=F(88, 700), fill=ink, anchor="ra")
    else:
        d.text((rx, 200), "Prices", font=F(64, 700), fill=ink, anchor="ra")
        d.text((rx, 276), "coming soon", font=F(40, 600), fill=ink, anchor="ra")

    # Bottom panel: top 3 by value.
    panel_y = 400
    d.rounded_rectangle([60, panel_y, W - 60, H - 50], radius=22, fill=(255, 255, 255, 235))
    if priced:
        d.text((90, panel_y + 26), "MOST VALUABLE", font=F(22, 700), fill="#6b6b66")
        col_w = (W - 180) / 3
        for i, (name, p) in enumerate(priced[:3]):
            x = 90 + i * col_w
            accent = c1 if c1 != "#ffcb05" else "#d62300"
            rank = f"#{i + 1}"
            d.text((x, panel_y + 64), rank, font=F(30, 700), fill=accent)
            nx = x + d.textlength(rank, font=F(30, 700)) + 12
            nf = fit(d, name, F, col_w - (nx - x) - 16, 30, 600, 20)
            d.text((nx, panel_y + 68), name, font=nf, fill="#1d1d1b")
            d.text((nx, panel_y + 108), money(p), font=F(36, 700), fill="#1d1d1b")
    else:
        d.text((90, panel_y + 80), "Tracking starts with the first daily eBay update.", font=F(32, 600), fill="#1d1d1b")
    return img


def site_image(data, order, F, site_label):
    img = gradient("#d62300", "#ffb703")
    dots(img, "#ffffff")
    d = ImageDraw.Draw(img, "RGBA")
    logo(d, 60, 50, 2.2)
    d.text((220, 125), "Toy Ticker", font=F(104, 700), fill="#ffffff", anchor="lm")
    d.text((62, 260), "Daily prices for McDonald's Happy Meal toys and cards", font=F(32, 500), fill="#ffffff")
    y = 330
    for sid in order:
        s = data["sets"][sid]
        c1, c2, ink = THEMES.get(sid, DEFAULT)
        rows = series_rows(data, sid)
        priced = [p for _, p in rows if p is not None]
        d.rounded_rectangle([60, y, W - 60, y + 76], radius=18, fill=(255, 255, 255, 235))
        d.rounded_rectangle([60, y, 76, y + 76], radius=8, fill=c1)
        d.text((100, y + 38), s["name"], font=F(34, 600), fill="#1d1d1b", anchor="lm")
        d.text((W - 90, y + 38), money(sum(priced)) if priced else "coming soon", font=F(34, 700), fill="#1d1d1b", anchor="rm")
        y += 92
    return img


SHARE_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta property="og:type" content="website">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{image}">
<meta property="og:url" content="{url}">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="{target}">
<meta http-equiv="refresh" content="0; url=../../?s={sid}">
<style>body{{font:16px system-ui,sans-serif;margin:40px;color:#1d1d1b;background:#fafaf7}}</style>
</head><body>
<p>Opening <a href="../../?s={sid}">{name}</a>…</p>
<script>location.replace("../../?s={sid}");</script>
</body></html>
"""


def build(data, root, site_url):
    root = Path(root)
    F = Fonts(root / "assets" / "fonts" / "Fredoka.ttf")
    site_label = "Toy Ticker"
    today = date.today().isoformat()
    (root / "og").mkdir(exist_ok=True)
    order = sorted(data["sets"], key=lambda k: data["sets"][k].get("order", 0))
    for sid in order:
        s = data["sets"][sid]
        rows = series_rows(data, sid)
        series_image(s, sid, rows, today, F, site_label).save(root / "og" / f"{sid}.png", optimize=True)
        priced = [p for _, p in rows if p is not None]
        desc = (f"Full set {money(sum(priced))} at today's median eBay asking prices. " if priced else "") + \
            f"Daily eBay prices for all {len(rows)} McDonald's {s['name']} Happy Meal {'cards' if s.get('kind') == 'cards' else 'toys'}."
        page = root / "s" / sid / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(SHARE_PAGE.format(
            title=html.escape(f"{s['name']} · Toy Ticker"), desc=html.escape(desc), name=html.escape(s["name"]),
            image=f"{site_url}og/{sid}.png", url=f"{site_url}s/{sid}/", target=f"{site_url}?s={sid}", sid=sid))
    site_image(data, order, F, site_label).save(root / "og" / "site.png", optimize=True)
    return order
