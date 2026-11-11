# Toy Ticker

Happy Meal eBay price tracker.

Daily eBay prices for McDonald's Happy Meal collectibles: the current Pokémon TCG
cards plus the two toy series before it (Disney Villains Boo Buckets, SpongeBob × One Piece).

- **Site:** https://nglungfai.github.io/toy-ticker/ (one page per series: `?s=<series id>`)
- **Data:** asking prices of active eBay US Buy It Now listings (eBay Browse API), saved to `data/prices.json`.
  Each item keeps the median, 25th percentile and lowest ask plus the listing count; lots, sets,
  graded cards and multi-item listings are filtered out by title.
- **Series and items:** `data/config.json`. Add a series there to start tracking it.
- **Updates:** `.github/workflows/update-prices.yml` runs `scripts/fetch_prices.py` daily.
  Needs repo secrets `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` (eBay developer Production keyset).

Run locally:

```bash
EBAY_CLIENT_ID=... EBAY_CLIENT_SECRET=... python3 scripts/fetch_prices.py
python3 -m http.server 8765   # then open http://localhost:8765
```
