#!/usr/bin/env python3
"""Small, sanitized A/B reproduction for Bright Data support ticket #713205.

Runs the exact Walmart nearByNodes request in two modes:

  minimal          No caller-supplied headers or cookies, as Bright Data advised.
  current_headers  The headers currently used by the production scraper.

The two modes are interleaved so they see roughly the same zone/target state. The
output intentionally excludes the proxy URL and request credentials.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

try:
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    sys.exit("Install requests first: pip install requests")


QUERY_HASH = "afe770a1a3a2856a44e153f01c7474896792e124bf562e142e0f8a89575f8f27"
BASE = "https://www.walmart.com/orchestra/home/graphql/nearByNodes/"
MODES = ("minimal", "current_headers")

CURRENT_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "x-apollo-operation-name": "nearByNodes",
    "x-o-platform": "rweb",
    "x-o-mart": "B2C",
    "x-o-bu": "WALMART-US",
    "x-o-segment": "oaoh",
    "x-o-platform-version": "us-web-1.0.0",
    "x-latency-trace": "1",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def variables(postal_code, state, product_id):
    return {
        "input": {
            "postalCode": postal_code,
            "accessTypes": ["PICKUP_INSTORE", "PICKUP_CURBSIDE"],
            "nodeTypes": ["STORE", "PICKUP_SPOKE", "PICKUP_POPUP"],
            "latitude": None,
            "longitude": None,
            "radius": None,
            "stateOrProvince": state,
            "productId": product_id,
            "maxCount": 50,
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


def build_url(postal_code, state, product_id):
    payload = json.dumps(variables(postal_code, state, product_id))
    return BASE + QUERY_HASH + "?variables=" + quote(payload)


def sanitize(value):
    """Remove credentials if a transport exception happens to include them."""
    if value is None:
        return None
    text = str(value)
    text = re.sub(r"https?://[^/@\s]+@", "http://[REDACTED]@", text)
    text = re.sub(
        r"brd-customer-[^:\s]+:[^@\s]+@",
        "brd-customer-[REDACTED]@[REDACTED]@",
        text,
        flags=re.IGNORECASE,
    )
    return text


def response_headers(response):
    keep = {"content-type", "content-length", "date", "server", "via"}
    out = {}
    for name, value in response.headers.items():
        lower = name.lower()
        if lower in keep or lower.startswith("x-brd-"):
            out[name] = sanitize(value)
    return out


def run_one(mode, index, url, proxy, timeout):
    headers = {}
    if mode == "current_headers":
        headers = dict(CURRENT_HEADERS)
        headers["x-o-correlation-id"] = f"bd-support-{uuid.uuid4().hex[:16]}"

    started = utc_now()
    start_clock = time.monotonic()
    record = {
        "mode": mode,
        "index": index,
        "started_utc": started,
        "method": "GET",
        "url": url,
        "caller_header_names": sorted(headers),
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
            verify=False,
        )
        body = response.text
        parsed = None
        json_error = None
        try:
            parsed = response.json()
        except Exception as exc:
            json_error = sanitize(exc)

        graphql_errors = parsed.get("errors") if isinstance(parsed, dict) else None
        nodes = None
        if isinstance(parsed, dict):
            near = ((parsed.get("data") or {}).get("nearByNodes") or {})
            nodes = near.get("nodes")

        record.update(
            {
                "finished_utc": utc_now(),
                "elapsed_seconds": round(time.monotonic() - start_clock, 3),
                "status": response.status_code,
                "reason": response.reason,
                "response_headers": response_headers(response),
                "x_brd_error": response.headers.get("x-brd-error"),
                "content_type": response.headers.get("content-type"),
                "body_bytes": len(response.content),
                "body_sha256": hashlib.sha256(response.content).hexdigest(),
                "body_sample": sanitize(body[:2000]),
                "json_parse_error": json_error,
                "graphql_errors": graphql_errors,
                "node_count": len(nodes) if isinstance(nodes, list) else None,
                "application_success": bool(
                    response.status_code == 200
                    and isinstance(parsed, dict)
                    and not graphql_errors
                    and isinstance(nodes, list)
                ),
            }
        )
    except Exception as exc:
        record.update(
            {
                "finished_utc": utc_now(),
                "elapsed_seconds": round(time.monotonic() - start_clock, 3),
                "status": None,
                "transport_error": sanitize(exc),
                "application_success": False,
            }
        )
    return record


def mode_summary(records, mode):
    selected = [row for row in records if row["mode"] == mode]
    statuses = Counter(str(row.get("status") or "TRANSPORT_ERROR") for row in selected)
    brd_errors = Counter(row.get("x_brd_error") or "none" for row in selected)
    app_successes = sum(bool(row.get("application_success")) for row in selected)
    json_responses = sum(row.get("json_parse_error") is None and row.get("status") is not None
                         for row in selected)
    return {
        "attempts": len(selected),
        "application_successes": app_successes,
        "application_success_rate": app_successes / len(selected) if selected else 0,
        "json_responses": json_responses,
        "status_counts": dict(sorted(statuses.items())),
        "x_brd_error_counts": dict(sorted(brd_errors.items())),
    }


def compact_failure(row):
    body = (row.get("body_sample") or "").replace("\r", " ").replace("\n", " ")
    return {
        "started_utc": row.get("started_utc"),
        "status": row.get("status"),
        "x_brd_error": row.get("x_brd_error"),
        "content_type": row.get("content_type"),
        "transport_error": row.get("transport_error"),
        "body_sample": body[:500],
    }


def write_outputs(output_dir, url, records, run_started, run_finished):
    output_dir.mkdir(parents=True, exist_ok=True)
    summaries = {mode: mode_summary(records, mode) for mode in MODES}
    payload = {
        "test": "Bright Data ticket #713205 A/B reproduction",
        "run_started_utc": run_started,
        "run_finished_utc": run_finished,
        "method": "GET",
        "url": url,
        "zone": "walmart_unlocker",
        "credential_data_included": False,
        "summaries": summaries,
        "responses": sorted(records, key=lambda row: (row["started_utc"], row["mode"])),
    }
    (output_dir / "results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )

    lines = [
        "Bright Data ticket #713205 - Walmart nearByNodes A/B test",
        f"Run: {run_started} to {run_finished}",
        "Method: GET",
        f"URL: {url}",
        "Zone: walmart_unlocker",
        "",
    ]
    for mode in MODES:
        item = summaries[mode]
        lines.extend(
            [
                f"{mode}:",
                f"  application success: {item['application_successes']}/{item['attempts']} "
                f"({item['application_success_rate']:.1%})",
                f"  status counts: {json.dumps(item['status_counts'], sort_keys=True)}",
                f"  x-brd-error counts: {json.dumps(item['x_brd_error_counts'], sort_keys=True)}",
                "",
            ]
        )
    (output_dir / "summary.txt").write_text("\n".join(lines), encoding="utf-8")

    reply = [
        "Hi Ofir,",
        "",
        "I ran an A/B test through the walmart_unlocker zone using the exact full "
        "nearByNodes URL from the production scraper. The two request types were "
        "interleaved so they saw approximately the same target and zone state.",
        "",
    ]
    for mode in MODES:
        item = summaries[mode]
        label = "No custom headers or cookies" if mode == "minimal" else "Current production headers"
        reply.extend(
            [
                f"{label}: {item['application_successes']}/{item['attempts']} application-level "
                f"successes ({item['application_success_rate']:.1%})",
                f"Status counts: {json.dumps(item['status_counts'], sort_keys=True)}",
                f"x-brd-error counts: {json.dumps(item['x_brd_error_counts'], sort_keys=True)}",
                "",
            ]
        )
    failures = [row for row in records if not row.get("application_success")]
    reply.extend(
        [
            f"Test window (UTC): {run_started} to {run_finished}",
            f"Exact URL: {url}",
            "",
            "I have attached the sanitized results.json file with timestamps, status codes, "
            "Bright Data error headers, content types, and response-body samples for every request.",
        ]
    )
    if failures:
        reply.extend(["", "Small failure sample:"])
        for row in failures[:4]:
            reply.append(json.dumps(compact_failure(row), sort_keys=True))
    reply.extend(["", "Thanks,", "Owen", ""])
    (output_dir / "support-reply.txt").write_text("\n".join(reply), encoding="utf-8")
    return summaries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests-per-mode", type=int, default=20)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--postal-code", default="60601")
    parser.add_argument("--state", default="IL")
    parser.add_argument("--product-id", default="5H2QX4ATI1DJ")
    parser.add_argument("--output-dir", default="brightdata_diagnostic")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.requests_per_mode <= 100:
        parser.error("--requests-per-mode must be between 1 and 100")
    if not 1 <= args.workers <= 100:
        parser.error("--workers must be between 1 and 100")

    url = build_url(args.postal_code, args.state, args.product_id)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "method": "GET",
                    "url": url,
                    "modes": {
                        "minimal": {"caller_headers": [], "cookies": []},
                        "current_headers": {"caller_headers": sorted(CURRENT_HEADERS), "cookies": []},
                    },
                    "requests_per_mode": args.requests_per_mode,
                    "workers": args.workers,
                    "network_requests_sent": 0,
                },
                indent=2,
            )
        )
        return

    proxy = os.environ.get("BRD_PROXY", "").strip()
    if not proxy:
        sys.exit("BRD_PROXY is not set. The diagnostic did not send any requests.")

    jobs = [(mode, index) for index in range(args.requests_per_mode) for mode in MODES]
    run_started = utc_now()
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(run_one, mode, index, url, proxy, args.timeout)
            for mode, index in jobs
        ]
        for future in concurrent.futures.as_completed(futures):
            records.append(future.result())

    run_finished = utc_now()
    summaries = write_outputs(Path(args.output_dir), url, records, run_started, run_finished)
    print(json.dumps(summaries, indent=2, sort_keys=True))
    print(f"Sanitized diagnostic files written to {args.output_dir}/")


if __name__ == "__main__":
    main()

