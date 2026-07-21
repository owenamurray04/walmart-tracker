#!/bin/bash
# ============================================================
#  Fix 3: probe-gated self-healing schedule. Each scheduled
#  firing sends ~10 gentle minutes of traffic; backs off if
#  Bright Data is still throttling, auto-runs the full sweep
#  the moment they recover. Double-click to apply.
# ============================================================
cd "$(dirname "$0")" || exit 1
FIX="fix3_2026-07-21"

echo ""
echo "  Applying fix 3 (probe-gated schedule)…"
echo "  --------------------------------"

if [ ! -d "$FIX" ]; then
  echo "  ✗ $FIX folder not found — nothing to apply."
  exit 1
fi

rm -f .git/index.lock .git/HEAD.lock .git/ORIG_HEAD.lock .git/objects/maintenance.lock

if ! git pull --rebase --autostash origin main; then
  echo ""
  echo "  ✗ Couldn't pull from GitHub — check your connection, then double-click again."
  exit 1
fi

cp "$FIX/weekly_check.py" weekly_check.py || exit 1
mkdir -p .github/workflows
cp "$FIX/walmart.yml" ".github/workflows/walmart.yml" || exit 1

git add weekly_check.py .github/workflows/walmart.yml
git commit -m "Probe-gated self-healing schedule (Mon-Wed 6h cron): back off while BD throttles, auto-sweep on recovery" \
  || echo "  (already committed)"
ok=false
for i in 1 2 3; do
  if git push origin HEAD:main; then ok=true; break; fi
  echo "  syncing… (attempt $i)"
  git pull --rebase --autostash origin main
  sleep 2
done

mkdir -p _to_delete
mv "$FIX" _to_delete/ 2>/dev/null
mv "Apply Fix 3.command" _to_delete/ 2>/dev/null

echo ""
if $ok; then
  echo "  ✅ Done. The schedule now takes care of itself:"
  echo "     • every 6h Mon–Wed it probes gently (~10 min)"
  echo "     • red X = Bright Data still throttled (cost ~zero)"
  echo "     • first green run = full sweep done + dashboard updated"
  echo "     Nothing to click, nothing to watch."
else
  echo "  ✗ Push failed. If it mentioned authentication, run:  gh auth login"
  echo "    then double-click this again."
fi
echo ""
read -n 1 -s -r -p "  Press any key to close…"
echo ""
