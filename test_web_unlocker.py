#!/usr/bin/env python3
"""
Bright Data Web Unlocker — validation test for Walmart nearByNodes
==================================================================
Before migrating the weekly job off patchright/DataImpulse, confirm that Web
Unlocker actually returns real store JSON for the nearByNodes GraphQL endpoint
(and not a PerimeterX/Akamai challenge page). This is a CHEAP, bounded test:
~50 requests, which fits inside Bright Data's free 5K/month tier → $0.

SETUP
  1. Sign up at brightdata.com → create a "Web Unlocker" zone.
  2. In the zone's Access Parameters, copy the proxy string. Then:
       export BRD_PROXY="http://brd-customer-<ID>-zone-<ZONE>:<PASS>@brd.superproxy.io:33335"
  3. pip install requests
  4. python test_web_unlocker.py            # 50 ZIPs, 1 product
     python test_web_unlocker.py --n 100    # bigger sample

WHAT PASS/FAIL MEANS
  - "ok" = request returned valid JSON with >=1 store node → the unlocker works.
  - "empty" = valid JSON but 0 nodes (ZIP genuinely has no nearby stores — rare).
  - "blocked/error" = challenge page, non-JSON, or transport error → the endpoint
    approach needs header tweaks (see notes printed at the end) before you commit.
  A healthy result is ~95%+ ok. If you see that, the migration is safe.
"""

import argparse, csv, json, os, random, sys, time
from urllib.parse import quote

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    sys.exit("Run: pip install requests")

# Same hash + product the live crawler uses for its API health check.
QUERY_HASH = "afe770a1a3a2856a44e153f01c7474896792e124bf562e142e0f8a89575f8f27"
TEST_PRODUCT = "2HZGMGX9CH7B"   # Elite-Rx Top (Teal)
BASE = "https://www.walmart.com/orchestra/home/graphql/nearByNodes/"

HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "x-apollo-operation-name": "nearByNodes",
    "x-o-platform": "rweb", "x-o-mart": "B2C", "x-o-bu": "WALMART-US",
    "x-o-segment": "oaoh", "x-o-platform-version": "us-web-1.0.0",
    "x-latency-trace": "1",
}


def variables(zip_code, state, product_id):
    return {
        "input": {
            "postalCode": zip_code,
            "accessTypes": ["PICKUP_INSTORE", "PICKUP_CURBSIDE"],
            "nodeTypes": ["STORE", "PICKUP_SPOKE", "PICKUP_POPUP"],
            "latitude": None, "longitude": None, "radius": None,
            "stateOrProvince": state, "productId": product_id, "maxCount": 50,
        },
        "checkItemAvailability": True, "checkWeeklyReservation": False,
        "enableStoreSelectorMarketplacePickup": False,
        "enableVisionStoreSelector": False,
        "enableStorePagesAndFinderPhase2": True,
        "enableStoreBrandFormat": False,
        "disableNodeAddressPostalCode": False,
        "enableWICStoreSelector": False,
    }


def load_zips(path, n):
    rows = [(r["zip"].strip(), r.get("state", "").strip())
            for r in csv.DictReader(open(path)) if r["zip"].strip()]
    random.shuffle(rows)
    return rows[:n]


def one_call(session, proxies, zip_code, state):
    url = BASE + QUERY_HASH + "?variables=" + quote(json.dumps(variables(zip_code, state, TEST_PRODUCT)))
    h = dict(HEADERS)
    h["x-o-correlation-id"] = "".join(random.choice("abcdefghijklmnop0123456789") for _ in range(12))
    try:
        r = session.get(url, headers=h, proxies=proxies, verify=False, timeout=60)
    except Exception as e:
        return "error", f"transport: {str(e)[:80]}", 0
    ctype = r.headers.get("content-type", "")
    if "application/json" not in ctype:
        # Almost always a challenge/interstitial HTML page.
        return "blocked", f"http {r.status_code}, content-type {ctype[:40] or 'none'}", 0
    try:
        j = r.json()
    except Exception:
        return "blocked", f"http {r.status_code}, non-JSON body", 0
    nodes = (((j.get("data") or {}).get("nearByNodes") or {}).get("nodes")) or []
    if nodes:
        return "ok", "", len(nodes)
    # Valid JSON but empty — could be a real gap, or a soft block returning {errors:[...]}.
    if j.get("errors"):
        return "blocked", f"graphql errors: {str(j['errors'])[:80]}", 0
    return "empty", "", 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50, help="how many ZIPs to test")
    ap.add_argument("--seeds", default="sample_zips.csv")
    ap.add_argument("--delay", type=float, default=0.3)
    args = ap.parse_args()

    proxy = os.environ.get("BRD_PROXY", "").strip()
    if not proxy:
        sys.exit("Set BRD_PROXY first (see the setup notes at the top of this file).")
    proxies = {"http": proxy, "https": proxy}

    zips = load_zips(args.seeds, args.n)
    print(f"Testing {len(zips)} ZIPs through Bright Data Web Unlocker "
          f"(1 product each = {len(zips)} requests)\n")

    counts = {"ok": 0, "empty": 0, "blocked": 0, "error": 0}
    stores = set()
    session = requests.Session()
    t0 = time.time()

    for i, (z, st) in enumerate(zips, 1):
        status, detail, n_nodes = one_call(session, proxies, z, st)
        counts[status] += 1
        if status == "ok":
            stores.add(z)  # placeholder; real crawl dedupes by store id
        tag = {"ok": "OK ", "empty": "-- ", "blocked": "BLK", "error": "ERR"}[status]
        line = f"  [{i:>3}/{len(zips)}] {tag} {z} {st}"
        if status == "ok":
            line += f"  ({n_nodes} stores)"
        elif detail:
            line += f"  {detail}"
        print(line)
        time.sleep(args.delay)

    dt = time.time() - t0
    total = len(zips)
    ok_rate = counts["ok"] / total * 100 if total else 0
    billable = counts["ok"] + counts["empty"]   # Bright Data bills successful responses
    print("\n" + "=" * 60)
    print(f"RESULT  ok={counts['ok']}  empty={counts['empty']}  "
          f"blocked={counts['blocked']}  error={counts['error']}   ({dt:.0f}s)")
    print(f"Success rate: {ok_rate:.1f}%")
    print(f"Billable (successful) responses: ~{billable} "
          f"→ ~${billable / 1000 * 1.5:.4f} at PAYG $1.5/1k (free-tier covered)")

    if ok_rate >= 90:
        print("\n✅ PASS — Web Unlocker returns real store JSON. Migration is safe.")
        print("   Next: swap run_crawl() to fetch nearByNodes via this proxy instead")
        print("   of patchright, and trim products_tracked.csv to the 4 core products.")
    elif counts["ok"] > 0:
        print("\n⚠️  PARTIAL — some calls work. Likely a header/session nuance.")
        print("   Try enabling 'Custom headers' in the zone settings so the x-o-*")
        print("   headers pass through, and re-run.")
    else:
        print("\n❌ FAIL — no store JSON came back. The endpoint approach needs work:")
        print("   - Confirm the zone type is 'Web Unlocker' (not plain proxy).")
        print("   - In zone settings, ensure custom headers are allowed.")
        print("   - If it still fails, the fallback is Bright Data's Scraping")
        print("     Browser (renders the page) — pricier, but pass-through headers")
        print("     are a non-issue. Tell me and I'll wire that instead.")


if __name__ == "__main__":
    main()
