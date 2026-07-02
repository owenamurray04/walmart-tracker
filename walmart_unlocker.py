#!/usr/bin/env python3
"""
Walmart Doctor's Choice availability — Bright Data Web Unlocker edition
=======================================================================
Replaces the patchright/DataImpulse browser crawler with plain HTTP requests
routed through Bright Data's Web Unlocker (proxy mode), which solves Walmart's
PerimeterX/Akamai wall for us. Simpler, no headless Chrome, and it only bills on
success.

GOAL OF THIS RUN
  Get the most (stores x items) we can for a FIXED, one-time credit budget,
  without ever exceeding it. It does that in two phases inside one hard cap:

  PHASE 1 — DISCOVER (cheap): crawl breadth-first with ONE product. The nearby-
    store list is identical for every product, so 1 call/ZIP finds up to 50
    stores. Each discovered store's ZIP becomes a new ZIP to crawl, fanning out
    across the country until we plateau or hit the discovery sub-cap.

  PHASE 2 — ITEMS (the rest of the budget): compute the MINIMAL covering set of
    ZIPs that blankets every store found, then check as many additional items as
    the leftover budget allows over that set — core 4 products first, extras
    after. Phase-1's product is already known for every covering ZIP, so it's not
    re-billed.

HARD SAFETY: the run stops the instant billed (successful) calls reach --budget.
Set it a little under your remaining free credits.

SETUP
  export BRD_PROXY="http://brd-customer-hl_xxx-zone-walmart_unlocker:PASS@brd.superproxy.io:33335"
  pip install requests
  python walmart_unlocker.py --budget 4880

Writes the same dashboard files as before: all_stores.csv, zip_cov.json,
coverage_zips.csv, store_products.csv, product_summary.csv, state_summary.csv,
line_coverage.csv, history.csv
"""

import argparse, csv, json, os, random, sys, threading, time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote

try:
    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    sys.exit("Run: pip install requests")

QUERY_HASH = "afe770a1a3a2856a44e153f01c7474896792e124bf562e142e0f8a89575f8f27"
US_STORES = 4788
BASE = "https://www.walmart.com/orchestra/home/graphql/nearByNodes/"
CORE = ["et_nvy", "eb_blk", "pt_roy", "pb_blk"]   # the 4 products the README tracks
ALL_STORES, ZIP_COV, COVER_ZIPS = "all_stores.csv", "zip_cov.json", "coverage_zips.csv"

HEADERS = {
    "accept": "application/json", "content-type": "application/json",
    "x-apollo-operation-name": "nearByNodes", "x-o-platform": "rweb",
    "x-o-mart": "B2C", "x-o-bu": "WALMART-US", "x-o-segment": "oaoh",
    "x-o-platform-version": "us-web-1.0.0", "x-latency-trace": "1",
}


def variables(zip_code, state, product_id):
    return {"input": {"postalCode": zip_code,
            "accessTypes": ["PICKUP_INSTORE", "PICKUP_CURBSIDE"],
            "nodeTypes": ["STORE", "PICKUP_SPOKE", "PICKUP_POPUP"],
            "latitude": None, "longitude": None, "radius": None,
            "stateOrProvince": state, "productId": product_id, "maxCount": 50},
            "checkItemAvailability": True, "checkWeeklyReservation": False,
            "enableStoreSelectorMarketplacePickup": False,
            "enableVisionStoreSelector": False,
            "enableStorePagesAndFinderPhase2": True,
            "enableStoreBrandFormat": False,
            "disableNodeAddressPostalCode": False,
            "enableWICStoreSelector": False}


class Budget:
    """Thread-safe cap on BILLED (successful) calls. reserve() lets a worker
    start only if billed + in-flight is still under the cap, so successful calls
    can never exceed `cap` (in-flight at stop <= worker count)."""
    def __init__(self, cap):
        self.cap, self.billed, self.inflight, self.attempts = cap, 0, 0, 0
        self.lk = threading.Lock()

    def reserve(self):
        with self.lk:
            if self.billed + self.inflight >= self.cap:
                return False
            self.inflight += 1
            self.attempts += 1
            return True

    def release(self, ok):
        with self.lk:
            self.inflight -= 1
            if ok:
                self.billed += 1

    def spent(self):
        with self.lk:
            return self.billed


def fetch(session, proxies, zip_code, state, product_id, timeout):
    """One nearByNodes call. Returns (ok, [store dicts w/ 'stock'])."""
    url = BASE + QUERY_HASH + "?variables=" + quote(json.dumps(variables(zip_code, state, product_id)))
    h = dict(HEADERS)
    h["x-o-correlation-id"] = "".join(random.choice("abcdefghijklmnop0123456789") for _ in range(12))
    try:
        r = session.get(url, headers=h, proxies=proxies, verify=False, timeout=timeout)
        if "application/json" not in r.headers.get("content-type", ""):
            return False, []
        j = r.json()
    except Exception:
        return False, []
    nodes = (((j.get("data") or {}).get("nearByNodes") or {}).get("nodes")) or []
    out = []
    for n in nodes:
        a = n.get("address") or {}
        prod = n.get("product") or {}
        out.append({"id": str(n.get("id")), "name": n.get("displayName") or "",
                    "city": a.get("city") or "", "state": a.get("state") or "",
                    "postal": a.get("postalCode") or "",
                    "stock": 1 if prod.get("availabilityStatus") == "IN_STOCK" else 0})
    return True, out


def make_pool(args):
    proxy = os.environ.get("BRD_PROXY", "").strip()
    if not proxy:
        sys.exit("Set BRD_PROXY first (the zone's Native proxy-based access string).")
    return {"http": proxy, "https": proxy}


# ---------------- PHASE 1: discovery ----------------
def discover(args, proxies, budget, disc_pid, seeds):
    stores, zip_cov, zip_state = {}, {}, {}
    seen = set(z for z, _ in seeds)
    frontier = deque(seeds)
    retry = defaultdict(int)
    lk = threading.Lock()
    disc_cap = min(args.disc_cap, args.budget)

    def handle(z, st, ok, rows):
        if not ok:
            with lk:
                if retry[z] < 3:
                    retry[z] += 1
                    frontier.append((z, st))
            return
        with lk:
            zip_state.setdefault(z, st)
            zip_cov.setdefault(z, set())
            for s in rows:
                zip_cov[z].add(s["id"])
                rec = stores.setdefault(s["id"], {"id": s["id"], "name": s["name"],
                    "city": s["city"], "state": s["state"], "postal": s["postal"]})
                rec[disc_pid_key] = s["stock"]
                nz = s["postal"]
                if nz and nz not in seen and len(stores) < US_STORES:
                    seen.add(nz)
                    frontier.append((nz, s["state"]))

    print(f"PHASE 1 — discovery (1 product), sub-cap {disc_cap} calls, {len(frontier)} seed ZIPs")
    t0 = time.time()
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        while True:
            wave = []
            while frontier and budget.spent() < disc_cap:
                if not budget.reserve():
                    break
                wave.append(frontier.popleft())
            if not wave:
                break
            futs = {ex.submit(fetch, session, proxies, z, st, disc_pid, args.timeout): (z, st)
                    for z, st in wave}
            for fut in as_completed(futs):
                z, st = futs[fut]
                ok, rows = fut.result()
                budget.release(ok)
                handle(z, st, ok, rows)
            print(f"  stores {len(stores)}, queue {len(frontier)}, "
                  f"credits {budget.spent()}, {int(time.time()-t0)}s")
            if budget.spent() >= disc_cap or (not frontier):
                break

    with open(ALL_STORES, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["store_id", "store_name", "city", "state", "postal"])
        for s in stores.values():
            w.writerow([s["id"], s["name"], s["city"], s["state"], s["postal"]])
    json.dump({z: sorted(ids) for z, ids in zip_cov.items()}, open(ZIP_COV, "w"))
    print(f"PHASE 1 done: {len(stores)} stores, {budget.spent()} credits used.\n")
    return stores, zip_cov, zip_state


def covering_set(stores, zip_cov):
    pool = {z: set(ids) for z, ids in zip_cov.items()}
    uncovered, chosen = set(stores), []
    while uncovered:
        best = max(pool, key=lambda z: len(pool[z] & uncovered), default=None)
        if best is None or not (pool[best] & uncovered):
            break
        chosen.append(best); uncovered -= pool[best]
    return chosen  # greedy order: highest-coverage ZIPs first


# ---------------- PHASE 2: items over covering set ----------------
def check_items(args, proxies, budget, stores, cover, zip_state, item_products):
    pkeys = [p["key"] for p in item_products]
    print(f"PHASE 2 — checking {len(item_products)} more items over {len(cover)} covering ZIPs "
          f"(budget left {args.budget - budget.spent()} credits)")
    work = deque((z, zip_state.get(z, ""), p) for z in cover for p in item_products)
    retry = defaultdict(int)
    lk = threading.Lock()
    t0 = time.time()
    done = 0
    session = requests.Session()

    def handle(z, st, p, ok, rows):
        nonlocal done
        if not ok:
            with lk:
                if retry[(z, p["key"])] < 3:
                    retry[(z, p["key"])] += 1
                    work.append((z, st, p))
            return
        with lk:
            done += 1
            for s in rows:
                rec = stores.get(s["id"])
                if rec is not None:
                    rec[p["key"]] = s["stock"]

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        while True:
            wave = []
            while work and budget.spent() < args.budget:
                if not budget.reserve():
                    break
                wave.append(work.popleft())
            if not wave:
                break
            futs = {ex.submit(fetch, session, proxies, z, st, p["product_id"], args.timeout): (z, st, p)
                    for z, st, p in wave}
            for fut in as_completed(futs):
                z, st, p = futs[fut]
                ok, rows = fut.result()
                budget.release(ok)
                handle(z, st, p, ok, rows)
            print(f"  item-calls ok {done}, queue {len(work)}, credits {budget.spent()}, "
                  f"{int(time.time()-t0)}s")
            if budget.spent() >= args.budget or not work:
                break
    return pkeys


def write_outputs(stores, all_products, have_keys):
    recs = list(stores.values())
    for r in recs:
        for k in have_keys:
            r.setdefault(k, "")
        r["line_coverage"] = sum(1 for k in have_keys if r.get(k) == 1)
        r["carries_any"] = 1 if r["line_coverage"] > 0 else 0
    total = len(recs)
    prod = [p for p in all_products if p["key"] in have_keys]

    cols = ["store_id", "store_name", "city", "state", "postal"] + have_keys + ["line_coverage", "carries_any"]
    with open("store_products.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in sorted(recs, key=lambda x: (-x["line_coverage"], x.get("state", ""), x.get("city", ""))):
            w.writerow({c: (r["id"] if c == "store_id" else r.get("name", "") if c == "store_name"
                            else r.get(c, "")) for c in cols})

    with open("product_summary.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["key", "label", "stores_in_stock", "in_stock_rate", "est_national"])
        for p in prod:
            ins = sum(1 for r in recs if r.get(p["key"]) == 1)
            w.writerow([p["key"], p["label"], ins, f"{ins/total:.4f}" if total else "0",
                        round(ins/total*US_STORES) if total else 0])

    by_state = defaultdict(lambda: [0, 0])
    for r in recs:
        by_state[r.get("state") or "?"][0] += 1
        by_state[r.get("state") or "?"][1] += r["carries_any"]
    with open("state_summary.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["state", "stores", "carrying_any"])
        for st in sorted(by_state):
            w.writerow([st, by_state[st][0], by_state[st][1]])

    dist = defaultdict(int)
    for r in recs:
        dist[r["line_coverage"]] += 1
    with open("line_coverage.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["products_in_stock", "store_count"])
        for k in range(len(have_keys) + 1):
            w.writerow([k, dist.get(k, 0)])

    carries = sum(r["carries_any"] for r in recs)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = not os.path.exists("history.csv")
    with open("history.csv", "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "stores_found", "carrying_any", "est_national_any"]
                       + [p["key"] + "_instock" for p in prod])
        w.writerow([now, total, carries, round((carries/total if total else 0)*US_STORES)]
                   + [sum(1 for r in recs if r.get(p["key"]) == 1) for p in prod])
    print(f"\nDONE: {total} stores, {carries} carry >=1 tracked item; "
          f"{len(have_keys)} items measured. Wrote dashboard CSVs.")


def main():
    global disc_pid_key
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=4880, help="hard cap on billed calls (< your free credits)")
    ap.add_argument("--disc-cap", type=int, default=3400, help="max calls to spend on store discovery")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--seeds", default="sample_zips.csv")
    ap.add_argument("--products", default="products_tracked.csv")
    ap.add_argument("--seed-from-known", action="store_true", default=True,
                    help="also seed from ZIPs in an existing store_products.csv")
    args = ap.parse_args()

    proxies = make_pool(args)
    all_products = list(csv.DictReader(open(args.products)))
    by_key = {p["key"]: p for p in all_products}
    # order: core 4 first (in README order), then the rest
    ordered = [by_key[k] for k in CORE if k in by_key] + [p for p in all_products if p["key"] not in CORE]
    disc_prod = ordered[0]                 # discovery uses a core product
    disc_pid = disc_prod["product_id"]
    disc_pid_key = disc_prod["key"]

    # seeds
    seen, seeds = set(), []
    for row in csv.DictReader(open(args.seeds)):
        z, st = row["zip"].strip(), row.get("state", "").strip()
        if z and z not in seen:
            seen.add(z); seeds.append((z, st))
    if args.seed_from_known and os.path.exists("store_products.csv"):
        for r in csv.DictReader(open("store_products.csv")):
            z, st = r.get("postal", "").strip(), r.get("state", "").strip()
            if z and z not in seen:
                seen.add(z); seeds.append((z, st))
    random.shuffle(seeds)

    budget = Budget(args.budget)
    stores, zip_cov, zip_state = discover(args, proxies, budget, disc_pid, seeds)

    cover = covering_set(stores, zip_cov)
    left = args.budget - budget.spent()
    # how many EXTRA items (beyond the discovery product) can we afford over the cover?
    n_extra = left // max(1, len(cover))
    # guarantee the 4 core products even if it means trimming the cover a bit
    min_extra = min(3, len(ordered) - 1)
    if n_extra < min_extra and len(cover) > 0:
        max_cover = left // max(1, min_extra)
        cover = cover[:max_cover]        # keep the highest-coverage ZIPs
        n_extra = min_extra
    n_extra = min(n_extra, len(ordered) - 1)
    item_products = [p for p in ordered if p["key"] != disc_pid_key][:n_extra]
    have_keys = [disc_pid_key] + [p["key"] for p in item_products]

    print(f"\nPLAN: covering set = {len(cover)} ZIPs; discovery product already known;\n"
          f"      + {n_extra} more items this run -> {len(have_keys)} items total "
          f"across up to {len(stores)} stores.\n")

    with open(COVER_ZIPS, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["zip", "state"])
        for z in cover:
            w.writerow([z, zip_state.get(z, "")])

    if item_products and cover:
        check_items(args, proxies, budget, stores, cover, zip_state, item_products)

    write_outputs(stores, all_products, have_keys)
    print(f"Total credits billed this run: {budget.spent()} / cap {args.budget}")


disc_pid_key = None
if __name__ == "__main__":
    main()
