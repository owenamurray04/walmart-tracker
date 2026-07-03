#!/usr/bin/env python3
"""
STORE-MAP REFRESH — run from GitHub Actions ("Store map refresh" workflow).
===========================================================================
Rebuilds the national store map from scratch by re-querying every ZIP we've
ever used (plus the gap seeds), so it:

  - PICKS UP new Walmart stores that opened since the last refresh
  - DROPS stores that closed (they vanish from the fresh crawl)
  - keeps the covering set honest for the weekly product check

Run it every month or two. Cost: ~6,000-8,500 billed calls (~$9-13).

Safety:
  - the live map files (all_stores.csv / zip_cov.json) are only overwritten
    when the refresh COMPLETES — a crashed or budget-stopped run changes
    nothing and resumes from its checkpoint without re-billing
  - hard budget cap on billed (successful) calls

Needs the BRD_PROXY environment variable (repo secret).
"""

import argparse, csv, json, os, sys, time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from walmart_unlocker import Budget, fetch, make_pool, CORE, ALL_STORES, ZIP_COV

try:
    import requests
except ImportError:
    sys.exit("Run: pip install requests")

STATE = "map_refresh_state.json"
STATE_MAX_AGE_H = 120


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=8500)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--wave", type=int, default=120)
    ap.add_argument("--gaps", default="gap_zips.csv")
    ap.add_argument("--products", default="products_tracked.csv")
    args = ap.parse_args()

    proxies = make_pool(args)
    by_key = {p["key"]: p for p in csv.DictReader(open(args.products))}
    disc_pid = by_key[CORE[0]]["product_id"]

    # old map — for the adds/drops report and the seed list
    old_ids = set()
    if os.path.exists(ALL_STORES):
        old_ids = {r["store_id"] for r in csv.DictReader(open(ALL_STORES))}
    seed_zips = []
    if os.path.exists(ZIP_COV):
        seed_zips += list(json.load(open(ZIP_COV)).keys())
    if os.path.exists(args.gaps):
        seed_zips += [r["zip"].strip() for r in csv.DictReader(open(args.gaps))]

    # fresh state (resume if a recent checkpoint exists)
    stores, zip_cov, seen, frontier = {}, {}, set(), deque()
    if os.path.exists(STATE):
        try:
            ck = json.load(open(STATE))
            if (time.time() - ck.get("ts", 0)) / 3600 <= STATE_MAX_AGE_H:
                stores = {s["id"]: s for s in ck["stores"]}
                zip_cov = {z: set(v) for z, v in ck["zip_cov"].items()}
                seen = set(ck["seen"])
                frontier = deque(ck["frontier"])
                print(f"resuming refresh: {len(stores)} stores, "
                      f"{len(zip_cov)} ZIPs done, {len(frontier)} queued")
        except Exception:
            print("unreadable checkpoint — starting fresh")
    if not frontier and not zip_cov:
        for z in dict.fromkeys(seed_zips):          # dedupe, keep order
            if z and z not in seen:
                seen.add(z)
                frontier.append(z)
        print(f"fresh refresh: {len(frontier)} seed ZIPs, "
              f"budget {args.budget} calls")

    budget = Budget(args.budget)
    retry = defaultdict(int)
    session = requests.Session()
    t0 = time.time()

    def save_state():
        tmp = STATE + ".tmp"
        json.dump({"ts": time.time(), "stores": list(stores.values()),
                   "zip_cov": {z: sorted(v) for z, v in zip_cov.items()},
                   "seen": sorted(seen), "frontier": list(frontier)},
                  open(tmp, "w"))
        os.replace(tmp, STATE)

    def handle(z, ok, rows):
        if not ok:
            if retry[z] < 2:
                retry[z] += 1
                frontier.append(z)
            return
        zip_cov.setdefault(z, set())
        for s in rows:
            zip_cov[z].add(s["id"])
            stores.setdefault(s["id"], {"id": s["id"], "name": s["name"],
                                        "city": s["city"], "state": s["state"],
                                        "postal": s["postal"]})
            nz = s["postal"]
            if nz and nz not in seen:            # follow brand-new stores
                seen.add(nz)
                frontier.appendleft(nz)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        while frontier and budget.spent() < args.budget:
            wave = []
            while frontier and len(wave) < args.wave and budget.reserve():
                wave.append(frontier.popleft())
            if not wave:
                break
            futs = {ex.submit(fetch, session, proxies, z, "", disc_pid,
                              args.timeout): z for z in wave}
            for fut in as_completed(futs):
                z = futs[fut]
                ok, rows = fut.result()
                budget.release(ok)
                handle(z, ok, rows)
            save_state()
            print(f"  stores {len(stores)}, queue {len(frontier)}, "
                  f"credits {budget.spent()}/{args.budget}, "
                  f"{int(time.time()-t0)}s")

    if frontier:
        save_state()
        sys.exit(f"PARTIAL: {len(zip_cov)} ZIPs done, {len(frontier)} left. "
                 f"Live map untouched. Re-run this workflow to resume "
                 f"without re-billing.")

    # complete — overwrite the live map atomically
    tmp = ALL_STORES + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["store_id", "store_name", "city", "state", "postal"])
        for s in stores.values():
            w.writerow([s["id"], s["name"], s["city"], s["state"], s["postal"]])
    os.replace(tmp, ALL_STORES)
    tmp = ZIP_COV + ".tmp"
    json.dump({z: sorted(v) for z, v in zip_cov.items()}, open(tmp, "w"))
    os.replace(tmp, ZIP_COV)
    if os.path.exists(STATE):
        os.remove(STATE)

    new_ids = set(stores)
    adds, drops = new_ids - old_ids, old_ids - new_ids
    print(f"\nDONE: {len(stores)} stores "
          f"(+{len(adds)} newly opened, -{len(drops)} closed/vanished vs "
          f"previous map). {budget.spent()} credits billed.")


if __name__ == "__main__":
    main()
