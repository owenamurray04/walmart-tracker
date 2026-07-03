# Doctor's Choice × Walmart — store-level availability tracker

Tracks which of Walmart's ~4,614 US stores stock the **Doctor's Choice** scrub
line (all 18 colorways), publishes a live dashboard, and accumulates the
true-carriage picture week over week.

**Dashboard:** https://owenamurray04.github.io/walmart-tracker/

**Everything runs from GitHub — nothing local.** Go to the **Actions tab**,
pick a workflow, press **Run workflow**:

| Actions-tab button | What it does | When | Cost |
|---|---|---|---|
| **1 · Weekly product check** | Checks the 11 in-store colorways at every mapped store, updates all dashboard data. | Weekly | ~$10.30 (set "Product list CSV" input to `products.csv` for a full 18-colorway audit, ~$16.80) |
| **2 · Store map refresh** | Rebuilds the store map — adds newly opened Walmarts, drops closed ones. | Every 1–2 months | ~$9–13 |
| pages build and deployment | GitHub's own site publisher — runs automatically after data commits. Never needs manual runs unless it fails (then: Re-run jobs). | automatic | free |

Both scrape workflows are **safe to re-run**: they checkpoint progress and
resume without re-billing anything already fetched. A crashed or
budget-capped run costs nothing extra.

To make the weekly check fully automatic (no button pressing), edit
`.github/workflows/walmart.yml` on github.com and uncomment the two
`schedule:` lines near the top.

## Setup (already done — for reference)

- Repo secret `BRD_PROXY` (Settings → Secrets and variables → Actions):
  the Bright Data Web Unlocker proxy URL,
  `http://brd-customer-…-zone-…:PASS@brd.superproxy.io:33335`.
  Billing is ~$1.50 per 1,000 *successful* calls; failures are free.
- GitHub Pages: Settings → Pages → Deploy from branch `main`, `/ (root)`.

## What the files are

| File | Role |
|---|---|
| `index.html` | The dashboard (GitHub Pages serves it). |
| `weekly_check.py` | Script behind workflow 1 — availability sweep over the covering ZIP set. |
| `refresh_map.py` | Script behind workflow 2 — rebuilds the store map. |
| `walmart_unlocker.py` | Shared library (HTTP fetch, budget cap, covering-set math). Original one-shot builder. |
| `gapfill_stores.py` | Legacy local gap-fill that built the initial map. Superseded by workflow 2. |
| `products_instore.csv` | The 11 shelved colorways — the weekly default. `products.csv` = all 18 incl. online-only (occasional audit). `products_tracked.csv` = legacy list with short keys. |
| `all_stores.csv` / `zip_cov.json` | The store map: 4,592 stores + which ZIP query reveals which stores. |
| `gap_zips.csv` | Rural seed ZIPs used by map refreshes to reach isolated stores. |
| `store_products.csv` | This week's per-store, per-product stock (the big output). |
| `ever_carried.csv` | Union across all runs — stores *ever* seen stocking each item. True carriage. |
| `carry_log.csv` | Per-run list of carrying stores → powers the adds/drops momentum stats. |
| `history.csv` / `product_history.csv` | Run-over-run trend data (dashboard ignores pre-July sampled runs). |
| `product_summary.csv`, `state_summary.csv`, `line_coverage.csv` | Per-product / per-state / depth rollups. |
| `states-10m.json` | US map geometry for the dashboard (self-hosted; auto-downloaded by workflow 1 if missing). |
| `*.command` files | **Legacy** Mac helpers from initial development. Not needed anymore. |

## Reading the numbers honestly

- Data source is Walmart's own store-pickup inventory — when it says a store
  has an item, precision is ~95–100%.
- A carrying store that's **sold out on check day shows as not stocking** —
  weekly counts are a floor. `ever_carried.csv` (the dashboard's headline
  number) converges on true carriage as weekly runs accumulate.
- "Online-only" colorways (gray, pink bottoms, ciel blue, wine) were checked
  at every store and found in ~0 — they're not part of the in-store line.
- Denominator: Walmart operates 4,614 US stores (corporate, Apr 2026);
  the map covers 4,592 (99.5%). Only ~3,920 (Supercenters + discount) are
  apparel formats.

## If something breaks

- **Run fails immediately with "Set BRD_PROXY"** → the repo secret is
  missing/renamed. Re-add it.
- **Run completes but found ~0 of everything** → Walmart redeployed and
  rotated their API's `QUERY_HASH`. Open any store page on walmart.com with
  browser dev tools → Network → filter `nearByNodes` → copy the long hash
  from the request URL → edit the `QUERY_HASH` line in `walmart_unlocker.py`
  directly on github.com. Happens a few times a year at most.
- **Green run but the site shows old data** → GitHub Pages deploy hiccup.
  Actions tab → the failed "pages build and deployment" → Re-run jobs.
  If deploys fail repeatedly: Settings → Pages → set Source to None, save,
  set back to `main` / root — this resets the deployment pipeline.
- **Bright Data balance** — check at brightdata.com; the free tier
  (5K calls/month) offsets ~$7.50 monthly.
