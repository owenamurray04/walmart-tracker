#!/bin/bash
# ============================================================
#  Run the Walmart tracker on THIS Mac (real fingerprint),
#  routed through DataImpulse so your home IP stays unblocked.
#  - Uses a separate, throwaway browser — never touches your Chrome.
#  - Drives that window internally; your mouse/keyboard stay yours.
#  - Only reads Walmart and writes data files in this folder.
#  You can keep using your computer normally while it runs.
# ============================================================
cd "$(dirname "$0")" || exit 1

echo ""
echo "  Walmart tracker — running locally (isolated browser, IP protected)"
echo "  =================================================================="

# proxy creds from a local, git-ignored file
[ -f proxy.txt ] && export DI_PROXY="$(tr -d '[:space:]' < proxy.txt)"
if [ -z "$DI_PROXY" ]; then
  echo "  ✗ Missing proxy.txt. Put your DataImpulse URL in a file named proxy.txt:"
  echo "      http://USER:PASS@gw.dataimpulse.com:823"
  read -r -p "  Press return to close." _; exit 1
fi

# one-time engine install
if ! python3 -c "import patchright" 2>/dev/null; then
  echo "  First run: installing the browser engine (one-time, ~1-2 min)…"
  python3 -m pip install --quiet patchright 2>/dev/null || pip3 install --quiet patchright
  python3 -m patchright install chromium
fi

count () { tail -n +2 all_stores.csv 2>/dev/null | wc -l | tr -d ' '; }
commit () {
  git add -A
  git commit -q -m "$1 $(date '+%m-%d %H:%M')" 2>/dev/null || return 0
  git pull --rebase --autostash -q origin main 2>/dev/null
  git push -q origin HEAD:main 2>/dev/null && echo "    ↳ pushed — dashboard will refresh"
}
run () { python3 -u walmart_store_check.py "$@" --seeds sample_zips.csv --bundled --delay 0.2; }

# --- Phase 1: discover every store (loops until the count stops growing) ---
if [ ! -f discovery_done ]; then
  prev=$(count); prev=${prev:-0}
  for pass in 1 2 3 4 5 6; do
    echo ""; echo "  Discovery pass $pass…  (a browser window will open — leave it be)"
    run --mode discover --max-minutes 30
    cur=$(count); cur=${cur:-0}
    echo "  → $cur stores found so far"
    commit "discovery: $cur stores"
    if [ "$cur" -gt 4000 ] && [ "$((cur - prev))" -lt 25 ]; then
      echo "  Discovery complete ($cur stores)."; touch discovery_done; commit "discovery complete"; break
    fi
    prev=$cur
  done
fi

# --- Phase 2: check all 11 colorways across every store ---
echo ""; echo "  Checking all 11 colorways across every store…"
run --mode weekly --max-minutes 60
commit "availability update"

echo ""
echo "  ✅ Done. Dashboard refreshes in ~1 minute. You can close this window."
read -r -p "  Press return to close." _
