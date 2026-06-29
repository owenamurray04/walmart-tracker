# Walmart availability tracker — Doctor's Choice scrubs

Tracks how widely the **Doctor's Choice** scrub line is stocked across U.S.
Walmart stores, refreshes weekly on autopilot, and publishes a live **dashboard**
to share with John.

**Latest read (Jun 29 2026, sampled):** of 1,666 stores checked, **193 stock at
least one of the 4 tracked products → ~555 stores nationwide**. The **Pro Fit**
line (royal-blue top / black bottom) is stocked ~3× more widely than **Elite-Rx**
(navy top / black bottom). Only 14 stores carry all four.

## What it tracks & why

The brand has 4 core products, each sold in several colors — but most colors
(gray, pink bottoms, wine, ciel blue) are **online-only and never stocked in
stores**. So we track the one **least-likely-to-be-sold-out color** of each, found
by measuring in-stock rates across colors:

| Product | Color tracked | `product_id` |
|---|---|---|
| Elite-Rx Top | Navy | 4ETHFRENRPHZ |
| Elite-Rx Bottom | Black | 13FORGOSKCZT |
| Pro Fit Top | Royal Blue | 1ZIJ3VQCTHCY |
| Pro Fit Bottom | Black | 17IWT6UXSUNY |

(`products.csv` lists all 18 color-variants discovered, for reference.)

## How full coverage works (no store database needed)

Walmart's `nearByNodes` API takes a ZIP and returns the 50 nearest stores — each
tagged with a product's stock status **and its own ZIP**. The scraper crawls:
start from seed ZIPs, and every store ZIP discovered becomes a new ZIP to query.
This breadth-first crawl fans out across the country until it has found every
store — bootstrapping nationwide coverage from the API itself. It checks all 4
products at each ZIP.

## Run it

1. **Double-click `Upload to GitHub.command`** → creates a public repo, pushes
   everything, turns on the dashboard (GitHub Pages).
2. **Add your proxy secret** (the script prints the link):
   Settings → Secrets and variables → Actions → `DI_PROXY` =
   `http://USER:PASS@gw.dataimpulse.com:823`.
3. **Run it once** from the Actions tab; then it runs every Monday.
4. **Copy the dashboard link for John**: `https://<you>.github.io/<repo>/`.

## Cost & runtime

Each call is ~30 KB of JSON. A full national crawl is ~4,000 store-ZIPs × 4
products ≈ 16k calls ≈ ~0.5 GB → **~$0.50/run** of DataImpulse bandwidth; runtime
~1–2 h (well within the Actions 6 h limit). GitHub compute + Pages are free on a
public repo.

## Files

| File | Purpose |
|---|---|
| `Upload to GitHub.command` | Double-click → publish/update everything. |
| `index.html` | Dashboard (Pages). |
| `walmart_store_check.py` | The crawler. |
| `products_tracked.csv` | The 4 products tracked weekly. |
| `products.csv` | All 18 color-variants (reference). |
| `sample_zips.csv` | 78 seed ZIPs to start the crawl. |
| `store_products.csv` | Per-store stock of each product (refreshed each run). |
| `product_summary.csv` / `state_summary.csv` / `line_coverage.csv` | Analysis rollups. |
| `history.csv` | One row per week → the trend. |
| `.github/workflows/walmart.yml` | Weekly run + commits results. |

## Analysis on the dashboard

- **Per-product reach** — how many stores stock each of the 4 (Pro Fit vs Elite-Rx).
- **Line coverage** — how many stores carry 0 / 1 / 2 / 3 / 4 of the line (i.e. how
  much of the lineup is actually selling through to shelves).
- **By state**, **trend over time**, and a table of the stores stocking the most.

## Maintenance

If runs start failing, refresh `QUERY_HASH` and the `product_id`s from your
browser's Network tab (filter `nearByNodes`) — Walmart changes these only on
redeploys.
