#!/usr/bin/env python3
"""
Walmart in-store availability checker  (lightweight / no-browser version)
========================================================================
Counts how many Walmart stores carry a given item for in-store pickup
("could you walk in and buy it").

THE CHEAP TRICK
---------------
Instead of loading a full ~7 MB product page per store, this hits Walmart's
internal store-selector API:

    GET /orchestra/home/graphql/nearByNodes/<hash>?variables=<json>

One call takes a ZIP and returns up to 50 nearby stores, EACH already tagged
with this item's availability:

    nodes[].id                       -> store number (e.g. "3341")
    nodes[].displayName              -> store name
    nodes[].address.{city,state,postalCode}
    nodes[].distance                 -> miles from the ZIP
    nodes[].product.availabilityStatus -> "IN_STOCK" | "OUT_OF_STOCK" | ...

`IN_STOCK` == in stock for in-store pickup at that store == "carries it".
Each response is tens of KB (not megabytes), so a full national sweep is a
few hundred calls and a few MB of bandwidth total — pennies through a proxy.

Validated live Jun 29 2026 (e.g. ZIP 60601 -> 6 nearby stores IN_STOCK).

WHY A PROXY IS STILL NEEDED ON GITHUB ACTIONS
---------------------------------------------
The endpoint sits behind PerimeterX. GitHub's runners use datacenter IPs that
PX blocks on sight, so route requests through DataImpulse (residential). Set:

    export DI_PROXY="http://USER:PASS@gw.dataimpulse.com:823"

If a call comes back as HTML instead of JSON, that's a PX challenge -> the
script retries, and if it persists you likely need fresher PX cookies
(see WALMART_COOKIE below) or a different proxy exit.

USAGE
    pip install requests
    python walmart_store_check.py --zips sample_zips.csv --out stores.csv

OUTPUT
    stores.csv : one row per UNIQUE store discovered
                 store_id, store_name, city, state, postal, status, in_store,
                 distance_mi, seed_zip, item_id, checked_at
    + a printed summary with the in-store count and national extrapolation.

MAINTENANCE
    Two values occasionally change when Walmart redeploys; if every call starts
    failing, refresh them from your browser's Network tab (filter "nearByNodes"):
      - QUERY_HASH : the long hex in the request URL
      - PRODUCT_ID : the "productId" inside the variables (NOT the usItemId)
"""

import argparse, csv, json, os, random, sys, time
from datetime import datetime, timezone
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("Run: pip install requests")

# ---- the item we track (Doctor's Choice Elite Rx scrub top) ----
ITEM_USID  = "15464001789"          # human-facing item id (for reference)
PRODUCT_ID = "2HZGMGX9CH7B"         # catalog id used by the API
QUERY_HASH = "afe770a1a3a2856a44e153f01c7474896792e124bf562e142e0f8a89575f8f27"

ENDPOINT = "https://www.walmart.com/orchestra/home/graphql/nearByNodes/" + QUERY_HASH
MAX_COUNT = 50                      # API caps at 50 stores per call

# Optional: paste a `Cookie:` header copied from a logged-out browser session
# if PerimeterX starts challenging. Leave "" to run cookieless.
WALMART_COOKIE = os.environ.get("WALMART_COOKIE", "")

HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "x-apollo-operation-name": "nearByNodes",
    "x-o-platform": "rweb",
    "x-o-mart": "B2C",
    "x-o-bu": "WALMART-US",
    "x-o-segment": "oaoh",
    "x-o-platform-version": "us-web-1.0.0",
    "x-latency-trace": "1",
    "referer": f"https://www.walmart.com/ip/{ITEM_USID}",
}
if WALMART_COOKIE:
    HEADERS["cookie"] = WALMART_COOKIE


def build_url(zip_code, state):
    variables = {
        "input": {
            "postalCode": zip_code,
            "accessTypes": ["PICKUP_INSTORE", "PICKUP_CURBSIDE"],
            "nodeTypes": ["STORE", "PICKUP_SPOKE", "PICKUP_POPUP"],
            "latitude": None, "longitude": None, "radius": None,
            "stateOrProvince": state,
            "productId": PRODUCT_ID,
            "maxCount": MAX_COUNT,
        },
        "checkItemAvailability": True,
        "checkWeeklyReservation": False,
        "enableStoreSelectorMarketplacePickup": False,
        "enableVisionStoreSelector": False,
        "enableStorePagesAndFinderPhase2": True,
        "enableStoreBrandFormat": False,
        "disableNodeAddressPostalCode": False,
        "enableWICStoreSelector": False,
    }
    return ENDPOINT + "?variables=" + quote(json.dumps(variables, separators=(",", ":")))


def fetch_zip(session, zip_code, state, proxies, retries=3):
    """Return list of node dicts for a ZIP, or [] on failure."""
    url = build_url(zip_code, state)
    for attempt in range(retries):
        try:
            h = dict(HEADERS)
            h["x-o-correlation-id"] = "".join(random.choices("abcdef0123456789", k=16))
            r = session.get(url, headers=h, proxies=proxies, timeout=30)
            ctype = r.headers.get("content-type", "")
            if "application/json" in ctype:
                data = r.json()
                node = (data.get("data") or {}).get("nearByNodes") or {}
                return node.get("nodes") or []
            # HTML body == PerimeterX challenge
        except Exception:
            pass
        time.sleep(2 + attempt * 3)   # backoff
    return None  # signals blocked/error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zips", default="sample_zips.csv")
    ap.add_argument("--out", default="stores.csv")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between calls")
    args = ap.parse_args()

    proxy = os.environ.get("DI_PROXY", "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if not proxy:
        print("WARNING: DI_PROXY not set — fine from a residential IP, but a "
              "datacenter/CI IP will likely be blocked by PerimeterX.")

    rows = list(csv.DictReader(open(args.zips)))[args.offset:]
    if args.limit:
        rows = rows[:args.limit]

    session = requests.Session()
    stores = {}        # store_id -> record (deduped across ZIPs)
    blocked_zips = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for i, row in enumerate(rows, 1):
        zc, st = row["zip"].strip(), row.get("state", "").strip()
        nodes = fetch_zip(session, zc, st, proxies)
        if nodes is None:
            blocked_zips += 1
            print(f"[{i}/{len(rows)}] {zc:<6} BLOCKED/err")
            time.sleep(args.delay)
            continue
        new = 0
        for n in nodes:
            sid = str(n.get("id"))
            if sid in stores:
                continue
            status = (n.get("product") or {}).get("availabilityStatus", "UNKNOWN")
            addr = n.get("address") or {}
            stores[sid] = {
                "store_id": sid,
                "store_name": n.get("displayName", ""),
                "city": addr.get("city", ""),
                "state": addr.get("state", ""),
                "postal": addr.get("postalCode", ""),
                "status": status,
                "in_store": status == "IN_STOCK",
                "distance_mi": n.get("distance", ""),
                "seed_zip": zc,
                "item_id": ITEM_USID,
                "checked_at": now,
            }
            new += 1
        print(f"[{i}/{len(rows)}] {zc:<6} {len(nodes):>2} stores "
              f"(+{new} new, total {len(stores)})")
        time.sleep(args.delay + random.uniform(0, 0.8))

    # write unique stores
    recs = list(stores.values())
    if recs:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(recs[0].keys()))
            w.writeheader()
            w.writerows(recs)

    total = len(recs)
    hits = sum(r["in_store"] for r in recs)
    rate = hits / total if total else 0
    print("\n----- SUMMARY -----")
    print(f"unique stores found : {total}")
    print(f"in-store (IN_STOCK) : {hits}")
    print(f"hit rate            : {rate:.1%}")
    print(f"blocked/err ZIPs    : {blocked_zips}/{len(rows)}")
    print(f"→ extrapolated to 4,788 US stores: ~{round(rate*4788):,} carry it")
    print(f"stores written to   : {args.out}")

    # per-state rollup (powers the dashboard's state chart)
    by_state = {}
    for r in recs:
        st = r["state"] or "?"
        d = by_state.setdefault(st, {"total": 0, "in_stock": 0})
        d["total"] += 1
        d["in_stock"] += 1 if r["in_store"] else 0
    with open("state_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["state", "total", "in_stock"])
        for st in sorted(by_state):
            w.writerow([st, by_state[st]["total"], by_state[st]["in_stock"]])

    # append a one-line weekly snapshot so the trend is visible over time
    hist = "history.csv"
    new_hist = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new_hist:
            w.writerow(["date", "unique_stores", "in_store", "hit_rate",
                        "est_national", "blocked_zips"])
        w.writerow([now, total, hits, f"{rate:.3f}",
                    round(rate * 4788), blocked_zips])


if __name__ == "__main__":
    main()
