#!/usr/bin/env python3
"""
Weekly availability sweep — every product, every store, no discovery cost.
==========================================================================
Uses the completed store map (all_stores.csv + zip_cov.json) and checks every
product in --products across the minimal covering set of ZIPs that blankets
all known stores. Designed for GitHub Actions:

  - NO discovery calls: the map is fixed (refresh it with gapfill_stores.py)
  - resumable: progress checkpoints to weekly_checkpoint.json every wave, so
    a crashed/timed-out run re-billed nothing when re-run within 5 days
  - hard budget cap on billed (successful) calls
  - outputs the same dashboard CSVs as before, PLUS:
      product_history.csv  — long-format per-product trend (fixed schema)
      ever_carried.csv     — union across runs: stores ever seen stocking
                             each product (true carriage, immune to stockouts)

Run:  export BRD_PROXY="http://...@brd.superproxy.io:33335"
      python3 weekly_check.py --budget 11000 --products products.csv
"""

import argparse, csv, json, os, sys, time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from walmart_unlocker import (Budget, fetch, make_pool, covering_set,
                              US_STORES, ALL_STORES, ZIP_COV)

try:
    import requests
except ImportError:
    sys.exit("Run: pip install requests")

CHECKPOINT = "weekly_checkpoint.json"
CHECKPOINT_MAX_AGE_H = 120          # resume checkpoints younger than 5 days


def atomic_write(path, writer_fn):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer_fn(f)
    os.replace(tmp, path)


def load_products(path):
    prods = []
    for r in csv.DictReader(open(path)):
        key = (r.get("key") or r["product_id"]).strip()
        prods.append({"key": key, "product_id": r["product_id"].strip(),
                      "label": r.get("label", key).strip()})
    return prods


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=12500)
    ap.add_argument("--products", default="products.csv")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--wave", type=int, default=120)
    args = ap.parse_args()

    proxies = make_pool(args)
    products = load_products(args.products)
    by_key = {p["key"]: p for p in products}

    stores = {}
    for r in csv.DictReader(open(ALL_STORES)):
        stores[r["store_id"]] = {"id": r["store_id"], "name": r["store_name"],
                                 "city": r["city"], "state": r["state"],
                                 "postal": r["postal"]}
    zip_cov = {z: set(ids) for z, ids in json.load(open(ZIP_COV)).items()}
    zip_state = {}
    for s in stores.values():
        zip_state.setdefault(s["postal"], s["state"])

    cover = covering_set(stores, zip_cov)
    total_calls = len(cover) * len(products)
    print(f"PLAN: {len(stores)} stores | covering set {len(cover)} ZIPs | "
          f"{len(products)} products -> {total_calls} calls "
          f"(~${total_calls*1.5/1000:.2f}), budget cap {args.budget}")

    # ---- resume from checkpoint if fresh ----
    results = {}                     # "zip|key" -> {store_id: 0/1}
    if os.path.exists(CHECKPOINT):
        try:
            ck = json.load(open(CHECKPOINT))
            age_h = (time.time() - ck.get("ts", 0)) / 3600
            if age_h <= CHECKPOINT_MAX_AGE_H:
                results = ck.get("results", {})
                print(f"resuming checkpoint: {len(results)} of {total_calls} "
                      f"calls already done ({age_h:.1f}h old)")
            else:
                print("checkpoint too old — starting fresh")
        except Exception:
            print("unreadable checkpoint — starting fresh")

    work = deque((z, p) for z in cover for p in products
                 if f"{z}|{p['key']}" not in results)
    retry = defaultdict(int)
    budget = Budget(args.budget)
    session = requests.Session()
    t0 = time.time()

    def save_checkpoint():
        atomic_write(CHECKPOINT,
                     lambda f: json.dump({"ts": time.time(),
                                          "results": results}, f))

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        while work and budget.spent() < args.budget:
            wave = []
            while work and len(wave) < args.wave and budget.reserve():
                wave.append(work.popleft())
            if not wave:
                break
            futs = {ex.submit(fetch, session, proxies, z,
                              zip_state.get(z, ""), p["product_id"],
                              args.timeout): (z, p) for z, p in wave}
            for fut in as_completed(futs):
                z, p = futs[fut]
                ok, rows = fut.result()
                budget.release(ok)
                if ok:
                    results[f"{z}|{p['key']}"] = {s["id"]: s["stock"]
                                                  for s in rows}
                else:
                    k = (z, p["key"])
                    if retry[k] < 3:
                        retry[k] += 1
                        work.append((z, p))
            save_checkpoint()
            done = len(results)
            print(f"  {done}/{total_calls} calls done, queue {len(work)}, "
                  f"credits {budget.spent()}/{args.budget}, "
                  f"{int(time.time()-t0)}s")

    if work:
        save_checkpoint()
        sys.exit(f"PARTIAL: {len(results)}/{total_calls} done, budget/queue "
                 f"stopped the run. Checkpoint saved — re-run to resume "
                 f"without re-billing. Not overwriting dashboard files.")

    # ---- complete: fold results into per-store availability ----
    have_keys = [p["key"] for p in products]
    for zk, per_store in results.items():
        key = zk.split("|", 1)[1]
        for sid, stock in per_store.items():
            rec = stores.get(sid)
            if rec is not None:
                rec[key] = stock

    recs = list(stores.values())
    for r in recs:
        for k in have_keys:
            r.setdefault(k, "")
        r["line_coverage"] = sum(1 for k in have_keys if r.get(k) == 1)
        r["carries_any"] = 1 if r["line_coverage"] > 0 else 0
    total = len(recs)
    carries = sum(r["carries_any"] for r in recs)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cols = (["store_id", "store_name", "city", "state", "postal"]
            + have_keys + ["line_coverage", "carries_any"])

    def w_store_products(f):
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(recs, key=lambda x: (-x["line_coverage"],
                                             x.get("state", ""),
                                             x.get("city", ""))):
            w.writerow({c: (r["id"] if c == "store_id"
                            else r.get("name", "") if c == "store_name"
                            else r.get(c, "")) for c in cols})
    atomic_write("store_products.csv", w_store_products)

    def w_product_summary(f):
        w = csv.writer(f)
        w.writerow(["key", "label", "stores_in_stock", "in_stock_rate",
                    "est_national"])
        for p in products:
            ins = sum(1 for r in recs if r.get(p["key"]) == 1)
            w.writerow([p["key"], p["label"], ins,
                        f"{ins/total:.4f}" if total else "0",
                        round(ins/total*US_STORES) if total else 0])
    atomic_write("product_summary.csv", w_product_summary)

    def w_state_summary(f):
        by_state = defaultdict(lambda: [0, 0])
        for r in recs:
            by_state[r.get("state") or "?"][0] += 1
            by_state[r.get("state") or "?"][1] += r["carries_any"]
        w = csv.writer(f)
        w.writerow(["state", "stores", "carrying_any"])
        for st in sorted(by_state):
            w.writerow([st, by_state[st][0], by_state[st][1]])
    atomic_write("state_summary.csv", w_state_summary)

    def w_line_coverage(f):
        dist = defaultdict(int)
        for r in recs:
            dist[r["line_coverage"]] += 1
        w = csv.writer(f)
        w.writerow(["products_in_stock", "store_count"])
        for k in range(len(have_keys) + 1):
            w.writerow([k, dist.get(k, 0)])
    atomic_write("line_coverage.csv", w_line_coverage)

    # history.csv: fixed 4-column rows only (per-product goes to
    # product_history.csv) — keeps the trend chart schema stable forever
    new = not os.path.exists("history.csv")
    with open("history.csv", "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "stores_found", "carrying_any",
                        "est_national_any"])
        w.writerow([now, total, carries,
                    round((carries/total if total else 0)*US_STORES)])

    new = not os.path.exists("product_history.csv")
    with open("product_history.csv", "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "key", "label", "stores_in_stock"])
        for p in products:
            ins = sum(1 for r in recs if r.get(p["key"]) == 1)
            w.writerow([now, p["key"], p["label"], ins])

    # carry_log.csv: per-run snapshot of carrying store ids (for the
    # dashboard's adds/drops momentum view)
    with open("carry_log.csv", "a", newline="") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["date", "store_id"])
        for r in recs:
            if r["carries_any"] == 1:
                w.writerow([now, r["id"]])

    # ever_carried.csv: union across all runs — true carriage floor
    ever = defaultdict(dict)
    if os.path.exists("ever_carried.csv"):
        for r in csv.DictReader(open("ever_carried.csv")):
            sid = r.pop("store_id")
            for k, v in r.items():
                if v == "1":
                    ever[sid][k] = 1
    for r in recs:
        for k in have_keys:
            if r.get(k) == 1:
                ever[r["id"]][k] = 1
    all_keys = sorted({k for d in ever.values() for k in d})

    def w_ever(f):
        w = csv.writer(f)
        w.writerow(["store_id"] + all_keys)
        for sid in sorted(ever):
            w.writerow([sid] + [ever[sid].get(k, "") for k in all_keys])
    atomic_write("ever_carried.csv", w_ever)
    ever_any = sum(1 for d in ever.values() if d)

    if os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)
    print(f"\nDONE: {total} stores checked for {len(products)} products; "
          f"{carries} in stock with >=1 item this week; "
          f"{ever_any} stores have EVER carried >=1 item. "
          f"Credits billed: {budget.spent()}.")


if __name__ == "__main__":
    main()
