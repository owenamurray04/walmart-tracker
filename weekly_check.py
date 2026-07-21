#!/usr/bin/env python3
"""
Weekly availability sweep — every product, every store, no discovery cost.
==========================================================================
Uses the completed store map (all_stores.csv + zip_cov.json) and checks every
product in --products across the minimal covering set of ZIPs that blankets
all known stores. Designed for GitHub Actions:

  - NO discovery calls: the map is fixed (refresh it with gapfill_stores.py)
  - resumable: progress checkpoints to weekly_checkpoint.json continuously, so
    a crashed/timed-out run re-bills nothing when re-run within 5 days
  - hard budget cap on billed (successful) calls
  - outputs the same dashboard CSVs as before, PLUS:
      product_history.csv  — long-format per-product trend (fixed schema)
      ever_carried.csv     — union across runs: stores ever seen stocking
                             each product (true carriage, immune to stockouts)

2026-07-21 rework (after the 07-20 run was throttled to a 78% failure rate):
Bright Data's unlocker layer rate-limits bursty, near-identical request
storms (`sr_rate_limit`). The old engine fired 120-call waves from 24
workers with instant retries — exactly that. The sweep now:
  - runs 8 workers (was 24), each launch spaced by a jittered gap (~0.35s)
  - slows down automatically when failures spike, speeds back up on success
  - retries failures up to 5x with exponential backoff (15s -> 4min)
  - shuffles the call order so identical GraphQL shapes don't cluster
  - REFUSES to overwrite dashboard files unless every planned call
    succeeded (a degraded day = "no update", never a phantom sellout)

Run:  export BRD_PROXY="http://...@brd.superproxy.io:33335"
      python3 weekly_check.py --budget 7500 --products products_instore.csv
"""

import argparse, csv, heapq, json, os, random, sys, time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, wait as fut_wait, FIRST_COMPLETED
from datetime import datetime, timezone

from walmart_unlocker import (Budget, fetch, make_pool, covering_set,
                              US_STORES, ALL_STORES, ZIP_COV)

try:
    import requests
except ImportError:
    sys.exit("Run: pip install requests")

CHECKPOINT = "weekly_checkpoint.json"
CHECKPOINT_MAX_AGE_H = 120          # resume checkpoints younger than 5 days


class Pacer:
    """Global launch throttle: consecutive request starts are spaced by a
    jittered gap that widens when calls fail (rate-limited) and relaxes back
    toward the base gap as calls succeed."""
    def __init__(self, base_gap=0.35, max_gap=8.0):
        import threading
        self.base, self.max, self.gap = base_gap, max_gap, base_gap
        self.next_t = 0.0
        self.lk = threading.Lock()

    def wait(self):
        with self.lk:
            now = time.time()
            slot = max(now, self.next_t)
            self.next_t = slot + self.gap * (0.7 + 0.6 * random.random())
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)

    def feedback(self, ok):
        with self.lk:
            if ok:
                self.gap = max(self.base, self.gap * 0.93)
            else:
                self.gap = min(self.max, self.gap * 1.35)


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
    ap.add_argument("--products", default="products_instore.csv")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--gap", type=float, default=0.35,
                    help="base seconds between request launches (jittered)")
    ap.add_argument("--max-gap", type=float, default=8.0,
                    help="ceiling the adaptive gap widens to under failures")
    ap.add_argument("--retries", type=int, default=5,
                    help="max retries per call, with exponential backoff")
    ap.add_argument("--probe-minutes", type=float, default=0,
                    help="start with N minutes of single-file, slow-paced "
                         "calls; if the success rate in that window is poor, "
                         "exit immediately (~100 gentle attempts) instead of "
                         "grinding. The unlocker's throttle is reputation-"
                         "based, so hammering a degraded pattern keeps it "
                         "degraded — probe, back off, try next firing. "
                         "0 = no probe.")
    ap.add_argument("--probe-gap", type=float, default=5.0,
                    help="seconds between launches during the probe window")
    ap.add_argument("--probe-pass", type=float, default=0.6,
                    help="probe success-rate needed to ramp to full speed")
    ap.add_argument("--fresh-window", type=float, default=0,
                    help="hours: exit immediately (doing nothing) if the last "
                         "completed sweep is newer than this and there is no "
                         "checkpoint to resume. Lets a dense cron schedule "
                         "chain partial runs into exactly one sweep per week.")
    args = ap.parse_args()

    # Chained-cron guard: if this week's sweep already completed and there's
    # nothing to resume, this firing has no work to do.
    if args.fresh_window > 0 and not os.path.exists(CHECKPOINT):
        try:
            last = open("history.csv").read().strip().splitlines()[-1].split(",")[0]
            age_h = (time.time()
                     - datetime.fromisoformat(last).timestamp()) / 3600
            if 0 <= age_h < args.fresh_window:
                print(f"Last completed sweep is {age_h:.0f}h old "
                      f"(< {args.fresh_window:.0f}h fresh-window) and no "
                      f"checkpoint to resume — this cycle's sweep is already "
                      f"done. Nothing to do.")
                return
        except Exception:
            pass                     # unreadable history — just run normally

    proxies = make_pool(args)
    products = load_products(args.products)

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
          f"(~${total_calls*1.5/1000:.2f}), budget cap {args.budget} | "
          f"{args.workers} workers, {args.gap}s base gap")

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

    todo = [(z, p) for z in cover for p in products
            if f"{z}|{p['key']}" not in results]
    random.shuffle(todo)             # de-cluster identical request shapes
    pending = deque(todo)
    deferred = []                    # heap of (ready_ts, seq, z, p) — backoff retries
    seq = 0
    attempts = defaultdict(int)
    dropped = 0
    # Probe gate: while probing, run single-file at probe pace; ramp to full
    # speed only once the window shows a healthy success rate.
    probing = args.probe_minutes > 0
    resumed_n = len(results)
    worker_cap = 1 if probing else args.workers
    pacer = Pacer(args.probe_gap if probing else args.gap, args.max_gap)
    if probing:
        print(f"PROBE: single-file at {args.probe_gap}s pace for "
              f"{args.probe_minutes:g} min — need >={args.probe_pass:.0%} "
              f"success to ramp up")
    budget = Budget(args.budget)
    session = requests.Session()
    t0 = time.time()
    last_save = last_note = time.time()

    def save_checkpoint():
        atomic_write(CHECKPOINT,
                     lambda f: json.dump({"ts": time.time(),
                                          "results": results}, f))

    def job(z, p):
        pacer.wait()                 # workers self-pace: launches never burst
        return fetch(session, proxies, z, zip_state.get(z, ""),
                     p["product_id"], args.timeout)

    inflight = {}                    # future -> (z, p)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        while pending or deferred or inflight:
            now = time.time()
            while deferred and deferred[0][0] <= now:
                _, _, z, p = heapq.heappop(deferred)
                pending.append((z, p))

            # Probe verdict: judge once the window has elapsed and there's a
            # meaningful sample (hard-stop the judgement at 3x the window).
            if probing:
                elapsed_min = (now - t0) / 60
                att = budget.attempts
                if ((elapsed_min >= args.probe_minutes and att >= 15)
                        or elapsed_min >= 3 * args.probe_minutes):
                    ok_n = len(results) - resumed_n
                    rate = ok_n / att if att else 0.0
                    if rate >= args.probe_pass:
                        print(f"PROBE PASSED: {ok_n}/{att} = {rate:.0%} — "
                              f"ramping to {args.workers} workers, "
                              f"{args.gap}s gap")
                        probing = False
                        worker_cap = args.workers
                        with pacer.lk:
                            pacer.base = pacer.gap = args.gap
                    else:
                        for fut in inflight:
                            fut.cancel()
                        save_checkpoint()
                        sys.exit(f"PROBE FAILED: {ok_n}/{att} calls "
                                 f"succeeded ({rate:.0%}) — the unlocker is "
                                 f"still throttling this pattern. Backing "
                                 f"off until the next scheduled firing "
                                 f"(~{att} gentle attempts, dashboard "
                                 f"untouched, checkpoint saved).")

            while pending and len(inflight) < worker_cap:
                if not budget.reserve():
                    break
                z, p = pending.popleft()
                inflight[ex.submit(job, z, p)] = (z, p)

            if not inflight:
                if (pending or deferred) and budget.spent() >= args.budget:
                    break            # budget exhausted with work remaining
                if not pending and not deferred:
                    break            # all done (or all dropped)
                nxt = (deferred[0][0] - time.time()) if deferred else 0.5
                time.sleep(min(1.0, max(0.05, nxt)))
                continue

            done_set, _ = fut_wait(set(inflight), timeout=5,
                                   return_when=FIRST_COMPLETED)
            for fut in done_set:
                z, p = inflight.pop(fut)
                ok, rows = fut.result()
                budget.release(ok)
                pacer.feedback(ok)
                key = f"{z}|{p['key']}"
                if ok:
                    results[key] = {s["id"]: s["stock"] for s in rows}
                else:
                    attempts[key] += 1
                    if attempts[key] <= args.retries:
                        backoff = min(90.0, 15.0 * (2 ** (attempts[key] - 1)))
                        backoff *= 0.75 + 0.5 * random.random()
                        seq += 1
                        heapq.heappush(deferred,
                                       (time.time() + backoff, seq, z, p))
                    else:
                        dropped += 1

            now = time.time()
            if now - last_save > 45:
                save_checkpoint()
                last_save = now
            if now - last_note > 60:
                print(f"  {len(results)}/{total_calls} done | "
                      f"queue {len(pending) + len(deferred)} "
                      f"(+{len(inflight)} in flight) | dropped {dropped} | "
                      f"gap {pacer.gap:.2f}s | "
                      f"credits {budget.spent()}/{args.budget} | "
                      f"{int(now - t0)}s", flush=True)
                last_note = now

    # Publish guard: refuse to overwrite the dashboard unless EVERY planned
    # call succeeded. A call that exhausts its retries is dropped from the
    # queue, and every unfetched (store, product) cell would count as
    # out-of-stock — a throttled day must read as "no update", not as a
    # nationwide sellout (see 2026-07-20).
    missing = [(z, p["key"]) for z in cover for p in products
               if f"{z}|{p['key']}" not in results]
    if missing:
        save_checkpoint()
        sys.exit(f"PARTIAL: {len(results)}/{total_calls} calls succeeded; "
                 f"{len(missing)} missing ({dropped} exhausted retries; "
                 f"budget spent {budget.spent()}/{args.budget}). Checkpoint "
                 f"saved — re-run to retry just the missing calls without "
                 f"re-billing successes. NOT overwriting dashboard files.")

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
