#!/usr/bin/env python3
"""
Walmart Doctor's Choice availability — full-coverage, multi-product crawler
===========================================================================
Covers (essentially) every U.S. Walmart store and checks the 4 tracked
Doctor's Choice products, then writes the data + analysis the dashboard uses.

HOW FULL COVERAGE WORKS (no store database needed)
--------------------------------------------------
Walmart's store-selector API (`nearByNodes`) takes a ZIP and returns the 50
nearest stores — each tagged with a product's stock status AND its own ZIP.
So we crawl: start from seed ZIPs, and every store ZIP we discover becomes a
new ZIP to query. Because each store's own ZIP returns that store plus its
neighbors, this breadth-first crawl fans out across the whole country and
discovers every store, bootstrapping coverage from the API itself.

For each ZIP we query all 4 tracked products (see products_tracked.csv), so we
learn, per store, which of the 4 are in stock.

WHAT IT WRITES
  store_products.csv  one row per store: id, name, city, state, zip,
                      a 1/0 column per product, line_coverage (0-4), carries_any
  product_summary.csv per product: stores_in_stock, in_stock_rate, est_national
  state_summary.csv   per state: stores, carrying_any
  line_coverage.csv   how many stores carry 0/1/2/3/4 of the line
  history.csv         one row per run (trend)

USAGE
  pip install requests
  export DI_PROXY="http://USER:PASS@gw.dataimpulse.com:823"   # needed on CI
  python walmart_store_check.py                      # full crawl
  python walmart_store_check.py --max-zips 400       # cap work (testing)
"""

import argparse, csv, json, os, random, sys, time
from collections import deque, defaultdict
from datetime import datetime, timezone
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("Run: pip install requests")

QUERY_HASH = "afe770a1a3a2856a44e153f01c7474896792e124bf562e142e0f8a89575f8f27"
ENDPOINT = "https://www.walmart.com/orchestra/home/graphql/nearByNodes/" + QUERY_HASH
MAX_COUNT = 50
US_STORES = 4788  # for national extrapolation

HEADERS = {
    "accept": "application/json", "content-type": "application/json",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "x-apollo-operation-name": "nearByNodes", "x-o-platform": "rweb",
    "x-o-mart": "B2C", "x-o-bu": "WALMART-US", "x-o-segment": "oaoh",
    "x-o-platform-version": "us-web-1.0.0", "x-latency-trace": "1",
}
if os.environ.get("WALMART_COOKIE"):
    HEADERS["cookie"] = os.environ["WALMART_COOKIE"]


def build_url(zip_code, state, product_id):
    v = {"input": {"postalCode": zip_code,
                   "accessTypes": ["PICKUP_INSTORE", "PICKUP_CURBSIDE"],
                   "nodeTypes": ["STORE", "PICKUP_SPOKE", "PICKUP_POPUP"],
                   "latitude": None, "longitude": None, "radius": None,
                   "stateOrProvince": state, "productId": product_id,
                   "maxCount": MAX_COUNT},
         "checkItemAvailability": True, "checkWeeklyReservation": False,
         "enableStoreSelectorMarketplacePickup": False,
         "enableVisionStoreSelector": False,
         "enableStorePagesAndFinderPhase2": True,
         "enableStoreBrandFormat": False,
         "disableNodeAddressPostalCode": False,
         "enableWICStoreSelector": False}
    return ENDPOINT + "?variables=" + quote(json.dumps(v, separators=(",", ":")))


def query(session, zip_code, state, product_id, proxies, retries=3):
    for attempt in range(retries):
        try:
            h = dict(HEADERS)
            h["x-o-correlation-id"] = "".join(random.choices("abcdef0123456789", k=16))
            r = session.get(build_url(zip_code, state, product_id), headers=h,
                            proxies=proxies, timeout=30)
            if "application/json" in r.headers.get("content-type", ""):
                node = (r.json().get("data") or {}).get("nearByNodes") or {}
                return node.get("nodes") or []
        except Exception:
            pass
        time.sleep(1.5 + attempt * 2)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="sample_zips.csv")
    ap.add_argument("--products", default="products_tracked.csv")
    ap.add_argument("--max-zips", type=int, default=0, help="0 = unlimited (crawl until done)")
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    proxy = os.environ.get("DI_PROXY", "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if not proxy:
        print("WARNING: DI_PROXY not set — a datacenter/CI IP will be blocked by PerimeterX.")

    products = list(csv.DictReader(open(args.products)))
    pkeys = [p["key"] for p in products]
    print(f"Tracking {len(products)} products: {', '.join(p['label'] for p in products)}")

    # BFS frontier of (zip, state)
    seen_zip = set()
    frontier = deque()
    for row in csv.DictReader(open(args.seeds)):
        z, st = row["zip"].strip(), row.get("state", "").strip()
        if z not in seen_zip:
            seen_zip.add(z); frontier.append((z, st))

    session = requests.Session()
    stores = {}          # id -> record
    n_queries = 0
    t0 = time.time()

    n_zips_done = 0
    while frontier:
        z, st = frontier.popleft()
        n_zips_done += 1
        for p in products:
            nodes = query(session, z, st, p["product_id"], proxies)
            n_queries += 1
            if not nodes:
                continue
            for n in nodes:
                sid = str(n.get("id"))
                a = n.get("address") or {}
                rec = stores.setdefault(sid, {
                    "store_id": sid, "store_name": n.get("displayName", ""),
                    "city": a.get("city", ""), "state": a.get("state", ""),
                    "postal": a.get("postalCode", "")})
                rec[p["key"]] = 1 if (n.get("product") or {}).get("availabilityStatus") == "IN_STOCK" else 0
                # discover new ZIPs for the crawl (unless we've hit the cap)
                nz = a.get("postalCode")
                cap_ok = (not args.max_zips) or (len(seen_zip) < args.max_zips)
                if nz and nz not in seen_zip and cap_ok:
                    seen_zip.add(nz); frontier.append((nz, a.get("state", "")))
            time.sleep(args.delay + random.uniform(0, 0.3))
        if n_zips_done % 50 == 0:
            print(f"  zips done {n_zips_done}/{len(seen_zip)} seen, queries {n_queries}, "
                  f"stores {len(stores)}, {int(time.time()-t0)}s")

    write_outputs(stores, products, pkeys)


def write_outputs(stores, products, pkeys):
    recs = list(stores.values())
    for r in recs:
        for k in pkeys:
            r.setdefault(k, "")
        vals = [r[k] for k in pkeys if r[k] != ""]
        r["line_coverage"] = sum(v for v in vals if v == 1)
        r["carries_any"] = 1 if r["line_coverage"] > 0 else 0

    cols = ["store_id", "store_name", "city", "state", "postal"] + pkeys + ["line_coverage", "carries_any"]
    with open("store_products.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in sorted(recs, key=lambda x: (x["state"], x["city"])):
            w.writerow({c: r.get(c, "") for c in cols})

    total = len(recs)
    # per-product summary
    with open("product_summary.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["key", "label", "stores_in_stock", "in_stock_rate", "est_national"])
        for p in products:
            ins = sum(1 for r in recs if r.get(p["key"]) == 1)
            rate = ins / total if total else 0
            w.writerow([p["key"], p["label"], ins, f"{rate:.4f}", round(rate * US_STORES)])

    # per-state
    by_state = defaultdict(lambda: [0, 0])
    for r in recs:
        by_state[r["state"] or "?"][0] += 1
        by_state[r["state"] or "?"][1] += r["carries_any"]
    with open("state_summary.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["state", "stores", "carrying_any"])
        for st in sorted(by_state):
            w.writerow([st, by_state[st][0], by_state[st][1]])

    # line coverage distribution
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
        rate_any = carries / total if total else 0
        w.writerow([now, total, carries, round(rate_any * US_STORES)]
                   + [sum(1 for r in recs if r.get(p["key"]) == 1) for p in products])

    print("\n----- SUMMARY -----")
    print(f"stores found       : {total}")
    print(f"carry >=1 product  : {carries} ({carries/total:.1%})" if total else "no stores")
    for p in products:
        ins = sum(1 for r in recs if r.get(p["key"]) == 1)
        print(f"  {p['label']:<28}: {ins} in stock  (~{round(ins/total*US_STORES) if total else 0} national)")
    print("wrote store_products.csv, product_summary.csv, state_summary.csv, "
          "line_coverage.csv, history.csv")


if __name__ == "__main__":
    main()
