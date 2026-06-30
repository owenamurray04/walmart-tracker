#!/usr/bin/env python3
"""
Walmart Doctor's Choice availability — full national coverage
=============================================================
Covers EVERY U.S. Walmart store, every run, by splitting the work into two phases.

PHASE 1 — DISCOVERY  (python walmart_store_check.py --mode discover)
  Crawls with ONE product (the nearest-50-stores list is identical regardless of
  color, so this is ~11x faster than checking every colorway). It finds every
  store and records which ZIP "sees" which stores, then computes a MINIMAL
  COVERING SET — the few hundred ZIPs whose results blanket all stores with no
  gaps. Writes:  all_stores.csv, zip_cov.json, coverage_zips.csv
  Run it until the printed coverage reads ~4,788 / 4,788 (1-3 runs); it
  accumulates, so each run only adds.

PHASE 2 — WEEKLY  (python walmart_store_check.py --mode weekly)  [default]
  Reads coverage_zips.csv and checks all 11 in-store colorways across that fixed
  set — a bounded run that touches every store. Writes the dashboard data:
  store_products.csv, product_summary.csv, state_summary.csv, line_coverage.csv,
  history.csv

Getting past PerimeterX: real Chrome (patchright) headful under xvfb, through a
DataImpulse sticky IP (one IP per session, re-warmed on rotation). See Session.

USAGE
  pip install patchright && patchright install --with-deps chrome
  export DI_PROXY="http://USER:PASS@gw.dataimpulse.com:823"
  xvfb-run python -u walmart_store_check.py --mode discover   # phase 1 (x1-3)
  xvfb-run python -u walmart_store_check.py                   # phase 2 (weekly)
"""

import argparse, csv, os, re, sys, time, secrets, tempfile, shutil, random, json
from collections import deque, defaultdict
from datetime import datetime, timezone

try:
    from patchright.sync_api import sync_playwright
except ImportError:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Run: pip install patchright && patchright install --with-deps chrome")

QUERY_HASH = "afe770a1a3a2856a44e153f01c7474896792e124bf562e142e0f8a89575f8f27"
WARMUP_URL = "https://www.walmart.com/ip/15464001789"
US_STORES = 4788
SESSION_MINUTES = 24
ALL_STORES, ZIP_COV, COVER_ZIPS = "all_stores.csv", "zip_cov.json", "coverage_zips.csv"

PAGE_JS = r"""
async ({zip, state, products, hash}) => {
  const headers = {'accept':'application/json','content-type':'application/json',
    'x-apollo-operation-name':'nearByNodes','x-o-platform':'rweb','x-o-mart':'B2C',
    'x-o-bu':'WALMART-US','x-o-segment':'oaoh','x-o-platform-version':'us-web-1.0.0',
    'x-latency-trace':'1'};
  const byId = {};
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
      const ac = new AbortController();
      const to = setTimeout(() => ac.abort(), 15000);
      let r;
      try {
        r = await fetch("/orchestra/home/graphql/nearByNodes/" + hash +
          "?variables=" + encodeURIComponent(JSON.stringify(v)),
          {headers, credentials:'include', signal: ac.signal});
      } finally { clearTimeout(to); }
      if (!(r.headers.get('content-type')||'').includes('application/json')) continue;
      const j = await r.json();
      const nodes = (j.data && j.data.nearByNodes && j.data.nearByNodes.nodes) || [];
      for (const n of nodes) {
        const id = String(n.id), a = n.address || {};
        const rec = byId[id] || (byId[id] = {id, name:n.displayName||'',
          city:a.city||'', state:a.state||'', postal:a.postalCode||''});
        rec[p.key] = (n.product && n.product.availabilityStatus) === 'IN_STOCK' ? 1 : 0;
      }
    } catch (e) {}
    await new Promise(r => setTimeout(r, 120));
  }
  return Object.values(byId);
}
"""


def parse_creds(di):
    m = re.match(r"https?://(?:([^:]+):([^@]+)@)?(.+)", di or "")
    return {"login": m.group(1), "pw": m.group(2), "hostport": m.group(3)} if m else None


def proxy_for(creds, sessid):
    if not creds:
        return None
    p = {"server": "http://" + creds["hostport"]}
    if creds["login"]:
        p["username"] = f"{creds['login']}__cr.us;sessid.{sessid}"
        p["password"] = creds["pw"]
    return p


class Session:
    def __init__(self, pw, creds, headless):
        self.pw, self.creds, self.headless = pw, creds, headless
        self.ctx = self.page = self.udd = None
        self.started = 0

    def open(self):
        sessid = secrets.token_hex(4)
        self.udd = tempfile.mkdtemp(prefix="wmctx_")
        self.ctx = self.pw.chromium.launch_persistent_context(
            self.udd, channel="chrome", headless=self.headless,
            proxy=proxy_for(self.creds, sessid), no_viewport=True, args=["--no-sandbox"])
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.started = time.time()
        ip = self._exit_ip()
        if not ip:
            print(f"  session {sessid}: PROXY not reachable")
            return False
        print(f"  session {sessid}: proxy OK, exit IP {ip}")
        return self._warm(sessid)

    def _exit_ip(self):
        try:
            self.page.goto("https://api.ipify.org/?format=json",
                           wait_until="domcontentloaded", timeout=10000)
            return json.loads(self.page.evaluate("document.body.innerText")).get("ip")
        except Exception as e:
            print(f"    proxy test failed: {str(e)[:100]}")
            return None

    def _api_works(self):
        """One real nearByNodes call against a known-busy ZIP. The true test of a
        session: not 'did the page load' but 'can we actually pull store data'."""
        try:
            res = self.page.evaluate(PAGE_JS, {"zip": "60601", "state": "IL",
                "products": [{"key": "_t", "product_id": "2HZGMGX9CH7B"}], "hash": QUERY_HASH})
            return len(res) > 0
        except Exception:
            return False

    def _warm(self, sessid):
        # Load the page so PerimeterX's sensor runs, then PROVE the API works.
        try:
            self.page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            # even if nav is slow/partial, the cookie may still be set — try the API anyway
            pass
        # Many IPs need a few extra seconds before PX grants API access — retry on
        # the SAME IP instead of throwing a usable session away.
        for wait in (3500, 3500, 4000):
            self.page.wait_for_timeout(wait)
            if self._api_works():
                print(f"  session {sessid}: API ready")
                return True
        print(f"  session {sessid}: warmed but API blocked")
        return False

    def close(self):
        try:
            self.ctx.close()
        except Exception:
            pass
        if self.udd:
            shutil.rmtree(self.udd, ignore_errors=True)

    def expired(self):
        return time.time() - self.started > SESSION_MINUTES * 60

    def query(self, zip_code, state, js_products):
        return self.page.evaluate(PAGE_JS, {"zip": zip_code, "state": state,
                                            "products": js_products, "hash": QUERY_HASH})


def crawl(sess, fresh, frontier, seen, js_products, t0, max_minutes, delay,
          on_result, expand=True, max_cap=0):
    """Shared crawl loop. on_result(zip, state, [store dicts]) handles each ZIP's
    stores. If expand, newly-seen store ZIPs are queued. Returns when frontier
    drains, time budget hits, or sessions can't recover."""
    empties = n = ok = 0
    retry = {}
    while frontier:
        if max_minutes and (time.time() - t0) > max_minutes * 60:
            print(f"  reached {max_minutes}-min budget — stopping"); break
        if sess.expired():
            print("  sticky IP window elapsed — rotating")
            if not fresh():
                print("  could not re-establish session — stopping"); break
            empties = 0
        z, st = frontier.popleft(); n += 1
        try:
            got = sess.query(z, st, js_products)
        except Exception:
            got = []
        if not got:
            empties += 1
            # never lose a ZIP to a transient block — requeue it for a healthy session
            c = retry.get(z, 0)
            if c < 4:
                retry[z] = c + 1; frontier.append((z, st))
            if empties >= 5:          # this session has gone bad — get a fresh IP
                print(f"  {empties} blanks — rotating session")
                if not fresh():
                    print("  sessions keep failing — stopping with what we have"); break
                empties = 0
        else:
            empties = 0; ok += 1
            on_result(z, st, got)
            if expand:
                for s in got:
                    nz = s.get("postal")
                    cap_ok = (not max_cap) or (len(seen) < max_cap)
                    if nz and nz not in seen and cap_ok:
                        seen.add(nz); frontier.append((nz, s.get("state", "")))
        if n % 50 == 0:
            print(f"  zips done {n}, ok {ok}, queue {len(frontier)}, seen {len(seen)}, {int(time.time()-t0)}s")
        sess.page.wait_for_timeout(int(delay * 1000))


# ---------- PHASE 1: discovery + covering-set ----------
def discover(args, products):
    js_products = [{"key": products[0]["key"], "product_id": products[0]["product_id"]}]
    print(f"DISCOVERY using 1 product ({products[0]['label']})")

    stores = {}                                  # id -> {id,name,city,state,postal}
    if os.path.exists(ALL_STORES):
        for r in csv.DictReader(open(ALL_STORES)):
            stores[r["store_id"]] = {"id": r["store_id"], "name": r["store_name"],
                "city": r["city"], "state": r["state"], "postal": r["postal"]}
    zip_cov = {}
    if os.path.exists(ZIP_COV):
        zip_cov = {z: set(ids) for z, ids in json.load(open(ZIP_COV)).items()}
    zip_state = {}
    print(f"  loaded {len(stores)} stores, {len(zip_cov)} covered ZIPs from prior runs")

    seen, seed_pairs = set(), []
    for row in csv.DictReader(open(args.seeds)):
        z, st = row["zip"].strip(), row.get("state", "").strip()
        if z and z not in seen:
            seen.add(z); seed_pairs.append((z, st)); zip_state[z] = st
    for r in stores.values():
        z, st = r["postal"], r["state"]
        if z and z not in seen:
            seen.add(z); seed_pairs.append((z, st)); zip_state.setdefault(z, st)
    random.shuffle(seed_pairs)
    frontier = deque(seed_pairs)

    def on_result(z, st, got):
        zip_state.setdefault(z, st)
        zip_cov.setdefault(z, set())
        for s in got:
            zip_cov[z].add(s["id"])
            stores[s["id"]] = {"id": s["id"], "name": s.get("name", ""),
                "city": s.get("city", ""), "state": s.get("state", ""), "postal": s.get("postal", "")}

    run_crawl(args, js_products, frontier, seen, on_result, expand=True)

    # persist master data
    with open(ALL_STORES, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["store_id", "store_name", "city", "state", "postal"])
        for s in stores.values():
            w.writerow([s["id"], s["name"], s["city"], s["state"], s["postal"]])
    json.dump({z: sorted(ids) for z, ids in zip_cov.items()}, open(ZIP_COV, "w"))

    # greedy minimal covering set over everything discovered so far
    all_ids = set(stores)
    pool = {z: set(ids) for z, ids in zip_cov.items()}
    uncovered, chosen = set(all_ids), []
    while uncovered:
        best = max(pool, key=lambda z: len(pool[z] & uncovered), default=None)
        if best is None or not (pool[best] & uncovered):
            break
        chosen.append(best); uncovered -= pool[best]
    with open(COVER_ZIPS, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["zip", "state"])
        for z in chosen:
            w.writerow([z, zip_state.get(z, "")])

    covered = len(all_ids) - len(uncovered)
    print(f"\nDISCOVERY: {len(stores)} stores known; covering set = {len(chosen)} ZIPs "
          f"covering {covered}/{len(all_ids)} of them.")
    if len(stores) < US_STORES * 0.9:
        print(f"  coverage still growing ({len(stores)}/~{US_STORES}). Run discover again to add more.")
    else:
        print(f"  looks complete (~{US_STORES}). You can switch to weekly mode now.")


# ---------- PHASE 2: weekly availability over the covering set ----------
def weekly(args, products):
    if not os.path.exists(COVER_ZIPS):
        sys.exit(f"{COVER_ZIPS} not found — run discovery first: --mode discover")
    pkeys = [p["key"] for p in products]
    js_products = [{"key": p["key"], "product_id": p["product_id"]} for p in products]
    print(f"WEEKLY checking {len(products)} colorways over the covering set")

    cover = [(r["zip"].strip(), r.get("state", "").strip())
             for r in csv.DictReader(open(COVER_ZIPS)) if r["zip"].strip()]
    random.shuffle(cover)   # if a run is time-capped, rotate which ZIPs get covered

    stores = {}
    if os.path.exists("store_products.csv"):
        for r in csv.DictReader(open("store_products.csv")):
            rec = {"store_id": r["store_id"], "store_name": r["store_name"],
                   "city": r["city"], "state": r["state"], "postal": r["postal"]}
            for k in pkeys:
                if r.get(k, "") in ("0", "1"):
                    rec[k] = int(r[k])
            stores[r["store_id"]] = rec

    seen = set(z for z, _ in cover)
    frontier = deque(cover)

    def on_result(z, st, got):
        for s in got:
            rec = stores.setdefault(s["id"], {"store_id": s["id"], "store_name": s.get("name", ""),
                "city": s.get("city", ""), "state": s.get("state", ""), "postal": s.get("postal", "")})
            for k in pkeys:
                if k in s:
                    rec[k] = s[k]

    run_crawl(args, js_products, frontier, seen, on_result, expand=False)
    write_outputs(stores, products, pkeys)


def run_crawl(args, js_products, frontier, seen, on_result, expand):
    creds = parse_creds(os.environ.get("DI_PROXY", "").strip())
    if not creds:
        print("WARNING: DI_PROXY not set — Walmart will block a cloud IP.")
    t0 = time.time()
    with sync_playwright() as pw:
        sess = Session(pw, creds, args.headless)

        def fresh():
            for a in range(12):          # keep hunting — good IPs are worth the wait
                if sess.ctx:
                    sess.close()
                if sess.open():
                    return True
            print("  12 IPs in a row failed warmup")
            return False

        if not fresh():
            print("\nERROR: could not get past the bot wall. NOT writing anything.")
            sys.exit(1)
        crawl(sess, fresh, frontier, seen, js_products, t0,
              args.max_minutes, args.delay, on_result, expand=expand)
        sess.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["discover", "weekly"], default="weekly")
    ap.add_argument("--seeds", default="sample_zips.csv")
    ap.add_argument("--products", default="products_tracked.csv")
    ap.add_argument("--max-minutes", type=int, default=80)
    ap.add_argument("--min-stores", type=int, default=100)
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    products = list(csv.DictReader(open(args.products)))
    if args.mode == "discover":
        discover(args, products)
    else:
        weekly(args, products)


def write_outputs(stores, products, pkeys):
    recs = list(stores.values())
    for r in recs:
        for k in pkeys:
            r.setdefault(k, "")
        r["line_coverage"] = sum(1 for k in pkeys if r[k] == 1)
        r["carries_any"] = 1 if r["line_coverage"] > 0 else 0
    if len(recs) < 100:
        print(f"\nERROR: only {len(recs)} stores — treating as a block, not overwriting.")
        sys.exit(1)

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
            w.writerow([p["key"], p["label"], ins, f"{ins/total:.4f}", round(ins/total*US_STORES)])

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
        w.writerow([now, total, carries, round((carries/total if total else 0)*US_STORES)]
                   + [sum(1 for r in recs if r.get(p["key"]) == 1) for p in products])
    print(f"\nWEEKLY: {total} stores, {carries} carry >=1 colorway. wrote 5 files.")


if __name__ == "__main__":
    main()
