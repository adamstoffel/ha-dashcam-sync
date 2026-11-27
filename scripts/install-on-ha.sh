#!/bin/sh
# Install or update Dashcam Sync on Home Assistant OS from a GitHub branch, tag or commit.
#
# Run it in the Terminal & SSH app (or Advanced SSH & Web Terminal):
#
#   wget -qO- https://raw.githubusercontent.com/adamstoffel/ha-dashcam-sync/main/scripts/install-on-ha.sh | sh -s -- <branch>
#
# It copies the app into the local apps folder (built on this machine, so no image is
# pulled) and the integration into custom_components, then gives the app a new version
# number so the Supervisor reloads its options and translations, and rebuilds it.
set -eu

REF="${1:-main}"
REPO="https://github.com/adamstoffel/ha-dashcam-sync"

apps="${DASHCAM_APPS_DIR:-}"
[ -n "$apps" ] || for dir in /local_apps /addons; do [ -d "$dir" ] && apps="$dir" && break; done
config="${DASHCAM_CONFIG_DIR:-}"
[ -n "$config" ] || for dir in /config /homeassistant; do [ -f "$dir/configuration.yaml" ] && config="$dir" && break; done
[ -n "$apps" ] || { echo "No local apps folder (/local_apps or /addons) in this terminal." >&2; exit 1; }
[ -n "$config" ] || { echo "No Home Assistant config folder (/config or /homeassistant)." >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# Resolve a branch or tag to its commit first: GitHub can serve a cached archive of a
# branch for minutes after a push, while an archive of a commit never changes.
sha="$(wget -qO- "https://api.github.com/repos/adamstoffel/ha-dashcam-sync/commits/$REF" 2>/dev/null \
  | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1)" || sha=""
if [ -n "$sha" ]; then
  echo "Downloading $REF at commit $(echo "$sha" | cut -c1-7) ..."
  wget -qO- "$REPO/archive/$sha.tar.gz" | tar xz -C "$tmp" --strip-components=1
else
  echo "Downloading $REF (could not look up its commit; the archive may lag a recent push) ..."
  wget -qO- "$REPO/archive/$REF.tar.gz" | tar xz -C "$tmp" --strip-components=1
fi

src_app="$tmp/app/dashcam_sync"
src_int="$tmp/custom_components/dashcam_sync"
for f in "$src_app/config.yaml" "$src_int/manifest.json"; do
  [ -f "$f" ] || { echo "The download for $REF has no ${f#"$tmp"/}; nothing was changed." >&2; exit 1; }
done

# Stage both next to their destinations (same file system, so the swap is a rename),
# edit the staged copy, then swap. The old copies stay until both are in place.
mkdir -p "$config/custom_components"
new_app="$apps/.dashcam_sync.new" new_int="$config/custom_components/.dashcam_sync.new"
old_app="$apps/.dashcam_sync.old" old_int="$config/custom_components/.dashcam_sync.old"
rm -rf "$new_app" "$new_int" "$old_app" "$old_int"
cp -r "$src_app" "$new_app"
cp -r "$src_int" "$new_int"

# Build here instead of pulling a published image, and always count as newer: a
# pre-release suffix like -dev would sort *before* the installed version.
cfg="$new_app/config.yaml"
base="$(sed -n 's/^version: "\{0,1\}\([0-9]*\.[0-9]*\)\..*/\1/p' "$cfg")"
sed -i '/^image:/d' "$cfg"
# Seconds in the patch number, and always above the installed one, so two installs in
# the same minute (or a skewed clock) still count as an update and rebuild.
patch="$(date +%y%m%d%H%M%S)"
installed="$(sed -n 's/^version: "\{0,1\}[0-9]*\.[0-9]*\.\([0-9]*\).*/\1/p' "$apps/dashcam_sync/config.yaml" 2>/dev/null || true)"
if [ -n "$installed" ] && [ "$installed" -ge "$patch" ] 2>/dev/null; then patch=$((installed + 1)); fi
sed -i "s/^version: .*/version: \"$base.$patch\"/" "$cfg"

restore() {
  echo "Install failed; putting the previous version back." >&2
  [ -d "$old_app" ] && { rm -rf "$apps/dashcam_sync"; mv "$old_app" "$apps/dashcam_sync"; }
  [ -d "$old_int" ] && { rm -rf "$config/custom_components/dashcam_sync"; mv "$old_int" "$config/custom_components/dashcam_sync"; }
  rm -rf "$new_app" "$new_int"
}
trap 'restore; rm -rf "$tmp"' INT TERM
{
  { [ ! -d "$apps/dashcam_sync" ] || mv "$apps/dashcam_sync" "$old_app"; } &&
  { [ ! -d "$config/custom_components/dashcam_sync" ] || mv "$config/custom_components/dashcam_sync" "$old_int"; } &&
  mv "$new_app" "$apps/dashcam_sync" &&
  mv "$new_int" "$config/custom_components/dashcam_sync"
} || { restore; exit 1; }
trap 'rm -rf "$tmp"' EXIT INT TERM
rm -rf "$old_app" "$old_int"
cfg="$apps/dashcam_sync/config.yaml"
echo "Installed $(grep '^version' "$cfg") from $REF${sha:+ ($(echo "$sha" | cut -c1-7))}"

if command -v ha >/dev/null 2>&1; then
  ha store reload >/dev/null
  if ha apps info local_dashcam_sync >/dev/null 2>&1; then
    echo "Updating the app (builds it; takes a minute) ..."
    ha apps update local_dashcam_sync
  else
    echo "Install it under Settings > Apps > App store > Local apps."
  fi
  echo "Then restart Home Assistant to load the integration, and hard-refresh the panel."
else
  echo "Now: Settings > Apps > App store > Check for updates, update Dashcam Sync, restart Home Assistant."
fi
