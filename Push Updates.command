#!/bin/bash
# ============================================================
#  Double-click to push the latest changes to GitHub.
#  No questions asked — it just commits everything and pushes.
#  (Run "Upload to GitHub.command" once first to create the repo.)
# ============================================================
cd "$(dirname "$0")" || exit 1

echo ""
echo "  Pushing updates to GitHub…"
echo "  --------------------------------"

if [ ! -d .git ] || ! git remote get-url origin >/dev/null 2>&1; then
  echo "  ✗ This folder isn't linked to GitHub yet."
  echo "    Double-click 'Upload to GitHub.command' first, then use this."
  echo ""
  exit 1
fi

git add -A
git commit -q -m "Update $(date '+%Y-%m-%d %H:%M')" || echo "  (no local changes to commit)"

# The GitHub Action commits data back to the repo, so the remote is usually
# ahead of your laptop. Pull-and-rebase first, then push (retry a few times).
ok=false
for i in 1 2 3 4 5; do
  if git pull --rebase --autostash origin main && git push origin HEAD:main; then ok=true; break; fi
  echo "  syncing… (attempt $i)"; sleep 2
done

echo ""
if $ok; then
  OWNER=$(gh api user --jq .login 2>/dev/null)
  REPO=$(basename -s .git "$(git remote get-url origin)")
  echo "  ✅ Pushed. Your dashboard will refresh in ~1 minute:"
  [ -n "$OWNER" ] && echo "     https://$OWNER.github.io/$REPO/"
else
  echo "  ✗ Push failed. If it mentions authentication, run:  gh auth login"
  echo "    (otherwise just double-click this again — it usually clears on a retry)"
fi
echo ""
