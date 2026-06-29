# Walmart in-store availability tracker — Doctor's Choice scrubs

Tracks how many of the ~4,788 U.S. Walmart stores carry the **Doctor's Choice
Elite Rx scrub top** for in-store pickup, refreshes weekly on autopilot, and
publishes a live **dashboard** you can share with John.

**Latest read (Jun 29 2026):** 49 of 1,666 sampled stores in stock (2.9%) →
**~141 stores nationwide** carrying this exact item. (See the caveat at the
bottom — this counts one colorway, so it's a floor, not the full brand presence.)

---

## Get it running (≈5 minutes, mostly clicking)

1. **Double-click `Upload to GitHub.command`.**
   It creates a public GitHub repo, pushes everything, and turns on the
   dashboard. (First time only, it'll have you install/log in to the GitHub CLI —
   it prints the exact commands.)

2. **Add your proxy as a secret** (the script prints the direct link):
   repo **Settings → Secrets and variables → Actions → New secret**
   - name: `DI_PROXY`
   - value: `http://USER:PASS@gw.dataimpulse.com:823`  ← your DataImpulse creds

3. **Run it once**: repo **Actions** tab → *Walmart store availability* →
   *Run workflow*. After that it runs itself every Monday.

4. **Copy the dashboard link for John**:
   `https://<your-username>.github.io/<repo-name>/`
   (the script prints the exact URL; it goes live ~1 minute after step 1).

---

## What's in here

| File | Purpose |
|---|---|
| `Upload to GitHub.command` | Double-click → publishes/updates everything. |
| `index.html` | The dashboard (GitHub Pages). Reads the CSVs below. |
| `walmart_store_check.py` | The scraper (plain `requests`, no browser). |
| `sample_zips.csv` | 78 ZIPs across all regions — the national sample. |
| `stores.csv` | In-stock stores (refreshed each run). |
| `state_summary.csv` | Per-state totals (powers the dashboard chart). |
| `history.csv` | One row per week → the trend line. |
| `.github/workflows/walmart.yml` | The weekly run + commits results back. |

## How it stays cheap

Instead of loading a 7 MB page per store, the scraper hits Walmart's store-
selector API (`nearByNodes`): **one ~30 KB JSON call returns up to 50 nearby
stores, each already tagged with this item's stock status.** A full sweep is a
few MB total — **well under $0.10/month** of DataImpulse bandwidth. GitHub
Actions compute and Pages hosting are free on a public repo.

The proxy is needed only because GitHub's runners use datacenter IPs that
Walmart's PerimeterX blocks; routing through DataImpulse (residential) fixes it.

## Coverage & accuracy notes

- **Sample vs. census.** The 78 metro ZIPs surface ~1,666 unique stores (each
  call covers ~50 within ~30 mi). Great for a trend and a national estimate. To
  count *every* store including rural ones, expand `sample_zips.csv` to ~400–600
  ZIPs — the scraper de-dupes, so more ZIPs just means fuller coverage.
- **One item, not the whole line.** We check a single listing (the Ceramic Teal
  top, `usItemId 15464001789`). A store that carries Doctor's Choice but is out
  of *this* top reads as out of stock — so ~141 is a **floor** on brand
  presence. To measure the brand properly, track several SKUs (colors/styles)
  and count a store if any are available. Easy to add — just more product IDs.

## Maintenance

If every run suddenly returns blocked/errors, two values in
`walmart_store_check.py` may need a refresh from your browser's Network tab
(filter `nearByNodes`): `QUERY_HASH` (hex in the URL) and `PRODUCT_ID` (inside
the variables). Walmart changes these only on redeploys — a 30-second copy-paste.
