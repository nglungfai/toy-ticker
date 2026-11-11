# Happy Meal Pokémon card prices

Daily price tracker for McDonald's Happy Meal Pokémon promo cards.

- **Data:** Cardmarket (EUR) prices from the free [TCGdex API](https://tcgdex.dev), saved to `data/prices.json`.
- **Updates:** `.github/workflows/update-prices.yml` runs `scripts/fetch_prices.py` daily and commits the new prices.
- **Sets tracked:** listed in `data/config.json`. Any "McDonald's Collection" set from `auto_from_year` on is added automatically when TCGdex publishes it.

Run locally:

```bash
python3 scripts/fetch_prices.py
python3 -m http.server 8765   # then open http://localhost:8765
```
