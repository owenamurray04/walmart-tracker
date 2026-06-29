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
if git diff --cached --quiet; then
  echo "  Nothing changed since the last push — you're up to date."
  echo ""
  exit 0
fi

git commit -q -m "Update $(date '+%Y-%m-%d %H:%M')"
if git push; then
  OWNER=$(gh api user --jq .login 2>/dev/null)
  REPO=$(basename -s .git "$(git remote get-url origin)")
  echo ""
  echo "  ✅ Pushed. Your dashboard will refresh in ~1 minute:"
  [ -n "$OWNER" ] && echo "     https://$OWNER.github.io/$REPO/"
else
  echo ""
  echo "  ✗ Push failed. If it mentions authentication, run:  gh auth login"
fi
echo ""
