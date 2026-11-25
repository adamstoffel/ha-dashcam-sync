#!/usr/bin/env bash
# Split this monorepo into the two release repositories described in the plan:
#   app/         -> dashcam-sync-app          (App repository, images on GHCR)
#   integration/ -> dashcam-sync-integration  (Marketplace / HACS catalog)
#
# Usage: scripts/split_repos.sh <github-owner>
# Creates the branches split/app and split/integration locally and prints the push commands.
set -euo pipefail
owner="${1:?usage: split_repos.sh <github-owner>}"
cd "$(dirname "$0")/.."

for pair in "app:dashcam-sync-app" "integration:dashcam-sync-integration"; do
  prefix="${pair%%:*}"; repo="${pair##*:}"
  git subtree split --prefix="$prefix" -b "split/$prefix" >/dev/null
  echo "git push git@github.com:${owner}/${repo}.git split/${prefix}:main"
done
echo
echo "Then tag both with the same version, e.g.:  git tag v\$(python scripts/check_versions.py | awk '{print \$3}')"
echo "The per-repo workflows live in app/.github and integration/.github and become active after the split."
