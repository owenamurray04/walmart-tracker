#!/usr/bin/env python3
"""
Gap-fill store discovery — finds the stores the main crawl couldn't reach.
===========================================================================
The breadth-first crawl spreads store-to-store, so it misses stores in
isolated pockets no crawled ZIP ever reaches. This script MERGES into the
existing map (all_stores.csv / zip_cov.json) instead of starting over:

  - seeds ONLY from gap_zips.csv (ZIPs >20mi from any known store,
    pre-sorted so the states with the biggest deficits go first)
  - skips every ZIP already queried (zip_cov.json)
  - BFS-expands from any new store it finds, like the main crawl
  - CHECKPOINTS after every wave: all_stores.csv + zip_cov.json are
    rewritten as it goes, so a crash or budget stop loses nothing
  - hard budget cap on billed (successful) calls

Run:  export BRD_PROXY="http://...@brd.superproxy.io:33335"
      python3 gapfill_stores.py --budget 300        # use what's left free
      python3 gapfill_stores.py --budget 2500       # full sweep (~$4)

Safe to re-run any number of times — it always resumes where it left off.
"""

import argparse, csv, json, os, sys, time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from walmart_unlocker import (Budget, fetch, make_pool, CORE,
                              ALL_STORES, ZIP_COV)

try:
    import requests
except ImportError:
    sys.exit("Run: pip install requests")


def load_map():
    stores = {}
    if os.path.exists(ALL_STORES):
        for r in csv.DictReader(open(ALL_STORES)):
            stores[r["store_id"]] = {"id": r["store_id"], "name": r["store_name"],
                                     "city": r["city"], "state": r["state"],
                                     "postal": r["postal"]}
    zip_cov = {}
    if os.path.exists(ZIP_COV):
        zip_cov = {z: set(ids) for z, ids in json.load(open(ZIP_COV)).items()}
    return stores, zip_cov


def save_map(stores, zip_cov):
    tmp = ALL_STORES + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["store_id", "store_name", "city", "state", "postal"])
        for s in stores.values():
            w.writerow([s["id"], s["name"], s["city"], s["state"], s["postal"]])
    os.replace(tmp, ALL_STORES)
    tmp = ZIP_COV + ".tmp"
    json.dump({z: sorted(ids) for z, ids in zip_cov.items()}, open(tmp, "w"))
    os.replace(tmp, ZIP_COV)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=300,
                    help="hard cap on billed (successful) calls this run")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=40)
    ap.add_argument("--gaps", default="gap_zips.csv")
    ap.add_argument("--products", default="products_tracked.csv")
    ap.add_argument("--wave", type=int, default=50,
                    help="calls per wave (checkpoint + progress print)")
    args = ap.parse_args()

    proxies = make_pool(args)
    by_key = {p["key"]: p for p in csv.DictReader(open(args.products))}
    disc_pid = by_key[CORE[0]]["product_id"]     # same product the crawl used

    stores, zip_cov = load_map()
    start_n = len(stores)
    seen = set(zip_cov)                          # never re-bill a queried ZIP
    frontier = deque()
    for r in csv.DictReader(open(args.gaps)):
        z = r["zip"].strip()
        if z and z not in seen:
            seen.add(z)
            frontier.append((z, r.get("state", "").strip()))
    print(f"resuming: {start_n} stores known, {len(zip_cov)} ZIPs queried, "
          f"{len(frontier)} gap seeds to try, budget {args.budget} calls")

    budget = Budget(args.budget)
    retry = defaultdict(int)
    session = requests.Session()
    t0 = time.time()

    def handle(z, st, ok, rows):
        if not ok:
            if retry[z] < 2:
                retry[z] += 1
                frontier.append((z, st))
            return 0
        zip_cov.setdefault(z, set())
        new = 0
        for s in rows:
            zip_cov[z].add(s["id"])
            if s["id"] not in stores:
                stores[s["id"]] = {"id": s["id"], "name": s["name"],
                                   "city": s["city"], "state": s["state"],
                                   "postal": s["postal"]}
                new += 1
                nz = s["postal"]
                if nz and nz not in seen:      # BFS from newly found stores
                    seen.add(nz)
                    frontier.appendleft((nz, s["state"]))
        return new

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        while frontier and budget.spent() < args.budget:
            wave = []
            while frontier and len(wave) < args.wave and budget.reserve():
                wave.append(frontier.popleft())
            if not wave:
                break
            futs = {ex.submit(fetch, session, proxies, z, st, disc_pid,
                              args.timeout): (z, st) for z, st in wave}
            found = 0
            for fut in as_completed(futs):
                z, st = futs[fut]
                ok, rows = fut.result()
                budget.release(ok)
                found += handle(z, st, ok, rows)
            save_map(stores, zip_cov)            # checkpoint every wave
            print(f"  stores {len(stores)} (+{len(stores)-start_n}), "
                  f"queue {len(frontier)}, credits {budget.spent()}/{args.budget}, "
                  f"{int(time.time()-t0)}s")

    save_map(stores, zip_cov)
    print(f"\nDONE: {len(stores)} stores (+{len(stores)-start_n} new), "
          f"{budget.spent()} credits billed. Map saved — safe to re-run "
          f"anytime to continue.")


if __name__ == "__main__":
    main()
