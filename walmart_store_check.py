#!/usr/bin/env python3
"""
Walmart Doctor's Choice availability — full-coverage, multi-product crawler
===========================================================================
Covers (essentially) every U.S. Walmart store and checks the 4 tracked
Doctor's Choice products, then writes the data + analysis the dashboard uses.

GETTING PAST THE BOT WALL (the two things that matter)
------------------------------------------------------
Walmart uses PerimeterX. To get data you need BOTH:
  1. A real browser that runs Walmart's JS challenge to earn an access cookie.
     -> we drive Chromium (Playwright), headful under a virtual display.
  2. A STABLE IP for the whole session. DataImpulse rotates IPs every request
     by default, which instantly invalidates that cookie. We pin one IP with a
     sticky `sessid` in the proxy username (refreshed every ~25 min, since a
     sticky IP lasts ~30 min), re-warming the cookie on each new IP.

HOW FULL COVERAGE WORKS (no store database needed)
--------------------------------------------------
`nearByNodes` takes a ZIP and returns the 50 nearest stores — each tagged with a
product's stock status AND its own ZIP. We crawl breadth-first: every store ZIP
discovered becomes a new ZIP to query, fanning out until every store is found.

SAFETY
  If the crawl finds fewer than --min-stores (default 100) it writes NOTHING and
  exits 1, so a blocked run can never overwrite good dashboard data.

USAGE
  pip install playwright && playwright install --with-deps chromium
  export DI_PROXY="http://USER:PASS@gw.dataimpulse.com:823"
  xvfb-run python walmart_store_check.py                 # full crawl (on CI)
  python walmart_store_check.py --max-zips 120 --headless  # quick local test
"""

import argparse, csv, os, re, sys, time, secrets
from collections import deque, defaultdict
from datetime import datetime, timezone

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Run: pip install playwright && playwright install --with-deps chromium")

QUERY_HASH = "afe770a1a3a2856a44e153f01c7474896792e124bf562e142e0f8a89575f8f27"
WARMUP_URL = "https://www.walmart.com/ip/15464001789"
US_STORES = 4788
SESSION_MINUTES = 24            # rotate sticky IP before the ~30-min limit
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

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
      const r = await fetch("/orchestra/home/graphql/nearByNodes/" + hash +
        "?variables=" + encodeURIComponent(JSON.stringify(v)),
        {headers, credentials:'include'});
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
    if not m:
        return None
    return {"login": m.group(1), "pw": m.group(2), "hostport": m.group(3)}


def proxy_for(creds, sessid):
    if not creds:
        return None
    p = {"server": "http://" + creds["hostport"]}
    if creds["login"]:
        # __cr.us pins US IPs; sessid.X keeps ONE IP sticky for ~30 min
        p["username"] = f"{creds['login']}__cr.us;sessid.{sessid}"
        p["password"] = creds["pw"]
    return p


class Session:
    """A browser bound to one sticky IP, warmed past PerimeterX."""
    def __init__(self, pw, creds, headless):
        self.pw, self.creds, self.headless = pw, creds, headless
        self.browser = self.ctx = self.page = None
        self.started = 0

    def open(self):
        sessid = secrets.token_hex(4)
        self.browser = self.pw.chromium.launch(
            headless=self.headless, proxy=proxy_for(self.creds, sessid),
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        self.ctx = self.browser.new_context(user_agent=UA, locale="en-US",
                                            viewport={"width": 1366, "height": 900})
        self.ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self.page = self.ctx.new_page()
        self.started = time.time()
        return self._warm(sessid)

    def _warm(self, sessid):
        try:
            self.page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(4000)  # let PerimeterX JS run
            ok = self.page.evaluate(
                "!!document.getElementById('__NEXT_DATA__') && /Doctor/i.test(document.title)")
            print(f"  session {sessid}: warmup {'OK' if ok else 'blocked'}")
            return bool(ok)
        except Exception as e:
            print(f"  session {sessid}: warmup error {type(e).__name__}")
            return False

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass

    def expired(self):
        return time.time() - self.started > SESSION_MINUTES * 60

    def query(self, zip_code, state, js_products):
        return self.page.evaluate(PAGE_JS, {"zip": zip_code, "state": state,
                                            "products": js_products, "hash": QUERY_HASH})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="sample_zips.csv")
    ap.add_argument("--products", default="products_tracked.csv")
    ap.add_argument("--max-zips", type=int, default=0)
    ap.add_argument("--min-stores", type=int, default=100)
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--headless", action="store_true", help="run headless (CI uses xvfb + headful)")
    args = ap.parse_args()

    creds = parse_creds(os.environ.get("DI_PROXY", "").strip())
    if not creds:
        print("WARNING: DI_PROXY not set / unparseable — Walmart will block a cloud IP.")

    products = list(csv.DictReader(open(args.products)))
    pkeys = [p["key"] for p in products]
    js_products = [{"key": p["key"], "product_id": p["product_id"]} for p in products]
    print(f"Tracking {len(products)} products")

    seen, frontier = set(), deque()
    for row in csv.DictReader(open(args.seeds)):
        z, st = row["zip"].strip(), row.get("state", "").strip()
        if z not in seen:
            seen.add(z); frontier.append((z, st))

    stores = {}
    t0 = time.time()

    with sync_playwright() as pw:
        sess = Session(pw, creds, args.headless)

        def fresh_session():
            for attempt in range(4):
                if sess.browser:
                    sess.close()
                if sess.open():
                    return True
                print(f"  warmup attempt {attempt+1} failed, rotating IP…")
            return False

        if not fresh_session():
            print("\nERROR: could not get past the bot wall (warmup never succeeded). "
                  "NOT overwriting existing data.")
            sys.exit(1)

        empties = 0; n_zip = 0
        while frontier:
            if sess.expired():
                print("  sticky IP window elapsed — rotating session")
                fresh_session()
            z, st = frontier.popleft(); n_zip += 1
            try:
                got = sess.query(z, st, js_products)
            except Exception:
                got = []

            if not got:
                empties += 1
                if empties >= 10:           # current IP likely flagged — rotate
                    print("  empty streak — rotating session")
                    fresh_session(); empties = 0
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
            sess.page.wait_for_timeout(int(args.delay * 1000))

        sess.close()

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
