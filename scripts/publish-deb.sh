#!/usr/bin/env bash
# Publish the newest built deb to the kid's laptop's flat apt repo.
#
# Delivery model: the dev machine builds + tests + publishes; the kid's laptop
# just runs apt. Its unattended-upgrades (Allowed-Origins "origin=*") then
# installs familymon updates automatically — same channel as every other
# package on that machine. No repo credentials, no build tools on the target.
#
#   ./scripts/publish-deb.sh --setup   # once: provision repo dir + sources
#                                      # entry on the target (needs sudo there)
#   ./scripts/publish-deb.sh           # publish newest dist/*.deb
#
# Repo layout (flat): /srv/familymon-apt/{*.deb,Packages,Packages.gz,Release}
# Target sources entry: deb [trusted=yes] file:/srv/familymon-apt ./
# The Release carries Origin/Suite "familymon" for origin-matching.
set -euo pipefail
export PATH="/usr/bin:/usr/local/bin:/bin:${PATH:-}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# The target is never hardcoded: set FAMILYMON_APT_HOST=user@host, or keep it in
# an untracked private/deploy.conf (sourced if present).
[ -f "$REPO/private/deploy.conf" ] && . "$REPO/private/deploy.conf"
TARGET_HOST="${FAMILYMON_APT_HOST:?set FAMILYMON_APT_HOST=user@host (the machine hosting the flat apt repo)}"
TARGET_DIR="/srv/familymon-apt"
LOCAL_REPO="$REPO/dist/repo"
KEEP=3  # most-recent debs to keep in the repo

if [ "${1:-}" = "--setup" ]; then
    echo "== one-time target setup on $TARGET_HOST =="
    ssh "$TARGET_HOST" "
        sudo install -d -o \$(id -un) -g \$(id -gn) $TARGET_DIR &&
        echo 'deb [trusted=yes] file:$TARGET_DIR ./' | sudo tee /etc/apt/sources.list.d/familymon.sources.list &&
        echo setup-done"
    exit 0
fi

# ---- assemble the flat repo locally ----------------------------------------
NEWEST="$(ls -t "$REPO"/dist/familymon_*_amd64.deb 2>/dev/null | head -1 || true)"
[ -n "$NEWEST" ] || { echo "no deb in dist/ — run scripts/build-deb.sh first"; exit 1; }
echo "publishing: $(basename "$NEWEST")"

mkdir -p "$LOCAL_REPO"
cp -n "$NEWEST" "$LOCAL_REPO/" 2>/dev/null || true
# prune old versions beyond KEEP
ls -t "$LOCAL_REPO"/familymon_*_amd64.deb 2>/dev/null | tail -n +$((KEEP + 1)) |
    while read -r old; do echo "pruning $(basename "$old")"; rm -f "$old"; done

cd "$LOCAL_REPO"
dpkg-scanpackages --multiversion . /dev/null > Packages
gzip -9kf Packages
# Flat-repo Release with checksums (apt-ftparchive without the apt-utils dep).
/usr/bin/python3 - <<'PYEOF'
import hashlib, os
from datetime import datetime, timezone

lines = [
    "Origin: familymon",
    "Label: familymon",
    "Suite: familymon",
    "Codename: familymon",
    "Architectures: amd64",
    "Description: familymon local apt repo (published from the dev machine)",
    "Date: " + datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC"),
]
files = ["Packages", "Packages.gz"]
for algo, title in (("md5", "MD5Sum"), ("sha256", "SHA256")):
    lines.append(title + ":")
    for name in files:
        digest = hashlib.new(algo, open(name, "rb").read()).hexdigest()
        lines.append(f" {digest} {os.path.getsize(name):>16} {name}")
open("Release", "w").write("\n".join(lines) + "\n")
PYEOF

# ---- push + refresh ----------------------------------------------------------
rsync -az --delete "$LOCAL_REPO/" "$TARGET_HOST:$TARGET_DIR/"
ssh "$TARGET_HOST" "sudo apt-get update -qq 2>&1 | grep -i familymon || true;
                    apt-cache policy familymon | head -6"
echo "published."
