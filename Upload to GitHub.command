#!/bin/bash
# ============================================================
#  Double-click this file to publish the tracker to GitHub.
#  It creates a public repo, pushes everything, and turns on
#  the dashboard (GitHub Pages). Run it again anytime to update.
# ============================================================
cd "$(dirname "$0")" || exit 1

echo ""
echo "  Walmart scrubs tracker — GitHub publisher"
echo "  ========================================="
echo ""

# --- 1. checks -------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  echo "  ✗ git isn't installed. Install Apple's command line tools with:"
  echo "      xcode-select --install"
  echo "    then double-click this file again."
  read -r -p "  Press return to close." _; exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "  ✗ GitHub CLI ('gh') isn't installed — it's what lets this script"
  echo "    create the repo for you. Install it with Homebrew:"
  echo "      brew install gh"
  echo "    (no Homebrew? get it at https://brew.sh) then run this again."
  read -r -p "  Press return to close." _; exit 1
fi

# --- 2. github login ------------------------------------------
if ! gh auth status >/dev/null 2>&1; then
  echo "  → You need to log in to GitHub (a browser window will open)."
  gh auth login || { echo "  Login cancelled."; read -r -p "  Press return to close." _; exit 1; }
fi

# --- 3. repo name ---------------------------------------------
DEFAULT="walmart-scrubs-tracker"
read -r -p "  Repo name [$DEFAULT]: " REPO
REPO=${REPO:-$DEFAULT}

# --- 4. commit -------------------------------------------------
[ -d .git ] || git init -q
git checkout -q -B main
git add .
git commit -q -m "Update Walmart scrubs tracker" || echo "  (nothing new to commit)"

# --- 5. create + push -----------------------------------------
if git remote get-url origin >/dev/null 2>&1; then
  echo "  → Pushing to existing repo…"
  git push -u origin main
else
  echo "  → Creating public repo '$REPO' and pushing…"
  gh repo create "$REPO" --public --source=. --remote=origin --push || {
    echo "  ✗ Could not create the repo (name may be taken). Try a different name.";
    read -r -p "  Press return to close." _; exit 1; }
fi

OWNER=$(gh api user --jq .login)

# --- 6. turn on GitHub Pages (dashboard) ----------------------
echo "  → Enabling the dashboard (GitHub Pages)…"
gh api -X POST "repos/$OWNER/$REPO/pages" \
   -f "source[branch]=main" -f "source[path]=/" >/dev/null 2>&1 \
   && echo "    ✓ Pages enabled" \
   || echo "    (Pages may already be on — that's fine)"

# --- 7. done ---------------------------------------------------
echo ""
echo "  ✅ Done!"
echo "  ------------------------------------------------------------"
echo "  Repo:      https://github.com/$OWNER/$REPO"
echo "  Dashboard: https://$OWNER.github.io/$REPO/   (live in ~1 min)"
echo ""
echo "  TWO things left, both in your browser:"
echo "   1) Add your proxy as a secret so the weekly run can scrape:"
echo "      https://github.com/$OWNER/$REPO/settings/secrets/actions"
echo "      New secret -> name: DI_PROXY"
echo "      value: http://USER:PASS@gw.dataimpulse.com:823"
echo "   2) Kick off the first run (then it's automatic every Monday):"
echo "      https://github.com/$OWNER/$REPO/actions  ->  'Walmart store availability'  ->  Run workflow"
echo ""
echo "  Share the Dashboard link above with John."
echo "  ------------------------------------------------------------"
read -r -p "  Press return to close." _
