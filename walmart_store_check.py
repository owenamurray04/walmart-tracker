#!/usr/bin/env python3
"""
Walmart Doctor's Choice availability — full-coverage, multi-product crawler
===========================================================================
Covers (essentially) every U.S. Walmart store and checks the 4 tracked
Doctor's Choice products, then writes the data + analysis the dashboard uses.

WHY A REAL BROWSER
------------------
Walmart's `nearByNodes` API is guarded by PerimeterX. Plain HTTP requests get
challenged and return nothing. A real browser executes Walmart's JS challenge,
earns the access cookie, and then same-origin fetches to the API succeed — so
this drives headless Chromium (Playwright) through your DataImpulse proxy and
runs the crawl via in-page fetch, exactly as it works in a normal browser.

HOW FULL COVERAGE WORKS (no store database needed)
--------------------------------------------------
`nearByNodes` takes a ZIP and returns the 50 nearest stores — each tagged with
a product's stock status AND its own ZIP. We crawl breadth-first: every store
ZIP we discover becomes a new ZIP to query, fanning out until every store is
found. Each ZIP is checked for all 4 tracked products.

OUTPUT
  store_products.csv, product_summary.csv, state_summary.csv,
  line_coverage.csv, history.csv   (see dashboard)

SAFETY
  If the crawl finds fewer than --min-stores (default 100) it is treated as a
  block/failure: it writes nothing and exits 1, so a bad run can never overwrite
  good dashboard data.

USAGE
  pip install playwright && playwright install --with-deps chromium
  export DI_PROXY="http://USER:PASS@gw.dataimpulse.com:823"
  python walmart_store_check.py                  # full crawl
  python walmart_store_check.py --max-zips 120   # quick test
"""

import argparse, csv, os, re, sys, time
from collections import deque, defaultdict
from datetime import datetime, timezone

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Run: pip install playwright && playwright install --with-deps chromium")

QUERY_HASH = "afe770a1a3a2856a44e153f01c7474896792e124bf562e142e0f8a89575f8f27"
WARMUP_URL = "https://www.walmart.com/ip/15464001789"
US_STORES = 4788
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# JS run inside the Walmart page: queries one ZIP for all products, returns
# merged store records + the ZIPs discovered. Mirrors the working browser crawl.
PAGE_JS = r"""
async ({zip, state, products, hash}) => {
  const headers = {'accept':'application/json','content-type':'application/json',
    'x-apollo-operation-name':'nearByNodes','x-o-platform':'rweb','x-o-mart':'B2C',
    'x-o-bu':'WALMART-US','x-o-segment':'oaoh','x-o-platform-version':'us-web-1.0.0',
    'x-latency-trace':'1'};
  const byId = {}; let okCalls = 0;
  for (const p of products) {
    const v = {input:{postalCode:zip,accessTypes:["PICKUP_INSTORE","PICKUP_CURBSIDE"],
      nodeTypes:["STORE","PICKUP_SPOKE","PICKUP_POPUP"],latitude:null,longitude:null,
      radius:null,stateOrProvince:state,productId:p.product_id,maxCount:50},
      checkItemAvailability:true,checkWeeklyReservation:false,
      enableStoreSelectorMarketplacePickup:false,enableVisionStoreSelector:false,
      enableStorePagesAndFinderPhase2:true,enableStoreBrandFormat:false,
      disableNodeAddressPostalCode:false,enableWICStoreSelector:false};
    headers['x-o-correlation-id'] = Math.random().toString(36).slice(2);
    try {
      const r = await fetch("/orchestra/home/graphql/nearByNodes/" + hash +
        "?variables=" + encodeURIComponent(JSON.stringify(v)),
        {headers, credentials:'include'});
      if (!(r.headers.get('content-type')||'').includes('application/json')) continue;
      const j = await r.json();
      const nodes = (j.data && j.data.nearByNodes && j.data.nearByNodes.nodes) || [];
      okCalls++;
      for (const n of nodes) {
        const id = String(n.id), a = n.address || {};
        const rec = byId[id] || (byId[id] = {id, name:n.displayName||'',
          city:a.city||'', state:a.state||'', postal:a.postalCode||''});
        rec[p.key] = (n.product && n.product.availabilityStatus) === 'IN_STOCK' ? 1 : 0;
      }
    } catch (e) {}
    await new Promise(r => setTimeout(r, 120));
  }
  return {okCalls, stores:Object.values(byId)};
}
"""


def make_proxy(di):
    m = re.match(r"https?://(?:([^:]+):([^@]+)@)?(.+)", di)
    if not m:
        return None
    user, pw, hostport = m.group(1), m.group(2), m.group(3)
    p = {"server": "http://" + hostport}
    if user:
        p["username"], p["password"] = user, pw
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="sample_zips.csv")
    ap.add_argument("--products", default="products_tracked.csv")
    ap.add_argument("--max-zips", type=int, default=0)
    ap.add_argument("--min-stores", type=int, default=100,
                    help="below this, treat as a block: write nothing, exit 1")
    ap.add_argument("--delay", type=float, default=0.25)
    args = ap.parse_args()

    di = os.environ.get("DI_PROXY", "").strip()
    proxy = make_proxy(di) if di else None
    if not proxy:
        print("WARNING: DI_PROXY not set / unparseable — Walmart will block a cloud IP.")

    products = list(csv.DictReader(open(args.products)))
    pkeys = [p["key"] for p in products]
    js_products = [{"key": p["key"], "product_id": p["product_id"]} for p in products]
    print(f"Tracking {len(products)} products: {', '.join(p['label'] for p in products)}")

    seen, frontier = set(), deque()
    for row in csv.DictReader(open(args.seeds)):
        z, st = row["zip"].strip(), row.get("state", "").strip()
        if z not in seen:
            seen.add(z); frontier.append((z, st))

    stores = {}
    t0 = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, proxy=proxy,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  viewport={"width": 1366, "height": 900})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()

        def warmup():
            page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)  # let PerimeterX JS run and set the cookie

        warmup()
        empties = 0; n_zip = 0

        while frontier:
            z, st = frontier.popleft(); n_zip += 1
            try:
                res = page.evaluate(PAGE_JS, {"zip": z, "state": st,
                                              "products": js_products, "hash": QUERY_HASH})
            except Exception:
                res = {"okCalls": 0, "stores": []}

            got = res.get("stores", [])
            if not got:
                empties += 1
                # a streak of empties usually means the PX cookie expired -> re-warm
                if empties in (8, 25, 60):
                    warmup()
            else:
                empties = 0
            for s in got:
                sid = s["id"]
                rec = stores.setdefault(sid, {"store_id": sid, "store_name": s.get("name", ""),
                    "city": s.get("city", ""), "state": s.get("state", ""), "postal": s.get("postal", "")})
                for k in pkeys:
                    if k in s:
                        rec[k] = s[k]
                nz = s.get("postal")
                cap_ok = (not args.max_zips) or (len(seen) < args.max_zips)
                if nz and nz not in seen and cap_ok:
                    seen.add(nz); frontier.append((nz, s.get("state", "")))

            if n_zip % 50 == 0:
                print(f"  zips {n_zip}/{len(seen)} seen, stores {len(stores)}, {int(time.time()-t0)}s")
            page.wait_for_timeout(int(args.delay * 1000))

        browser.close()

    if len(stores) < args.min_stores:
        print(f"\nERROR: only {len(stores)} stores found (< {args.min_stores}). "
              "Treating as a block — NOT overwriting existing data.")
        sys.exit(1)

    write_outputs(stores, products, pkeys)


def write_outputs(stores, products, pkeys):
    recs = list(stores.values())
    for r in recs:
        for k in pkeys:
            r.setdefault(k, "")
        r["line_coverage"] = sum(1 for k in pkeys if r[k] == 1)
        r["carries_any"] = 1 if r["line_coverage"] > 0 else 0

    cols = ["store_id", "store_name", "city", "state", "postal"] + pkeys + ["line_coverage", "carries_any"]
    with open("store_products.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in sorted(recs, key=lambda x: (-x["line_coverage"], x["state"], x["city"])):
            w.writerow({c: r.get(c, "") for c in cols})

    total = len(recs)
    with open("product_summary.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["key", "label", "stores_in_stock", "in_stock_rate", "est_national"])
        for p in products:
            ins = sum(1 for r in recs if r.get(p["key"]) == 1)
            rate = ins / total if total else 0
            w.writerow([p["key"], p["label"], ins, f"{rate:.4f}", round(rate * US_STORES)])

    by_state = defaultdict(lambda: [0, 0])
    for r in recs:
        by_state[r["state"] or "?"][0] += 1
        by_state[r["state"] or "?"][1] += r["carries_any"]
    with open("state_summary.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["state", "stores", "carrying_any"])
        for st in sorted(by_state):
            w.writerow([st, by_state[st][0], by_state[st][1]])

    dist = defaultdict(int)
    for r in recs:
        dist[r["line_coverage"]] += 1
    with open("line_coverage.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["products_in_stock", "store_count"])
        for k in range(len(pkeys) + 1):
            w.writerow([k, dist.get(k, 0)])

    carries = sum(r["carries_any"] for r in recs)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = not os.path.exists("history.csv")
    with open("history.csv", "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "stores_found", "carrying_any", "est_national_any"]
                       + [p["key"] + "_instock" for p in products])
        w.writerow([now, total, carries, round((carries / total if total else 0) * US_STORES)]
                   + [sum(1 for r in recs if r.get(p["key"]) == 1) for p in products])

    print(f"\nstores found {total}, carry >=1 {carries}. wrote 5 files.")


if __name__ == "__main__":
    main()
