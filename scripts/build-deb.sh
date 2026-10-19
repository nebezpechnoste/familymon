#!/usr/bin/env bash
# Build familymon_<version>_amd64.deb on the dev machine.
#
# The package bundles a relocatable uv venv at /opt/familymon/venv built
# --frozen from the committed uv.lock — the deployed machine gets exactly the
# dependency set that passed the test suite here, and never builds anything
# itself (no compiler, no PyPI access, no repo credentials on the kid's box).
#
# Contents:
#   /opt/familymon/venv                 bundled env + familymon (non-editable)
#   /usr/bin/familymonctl               thin wrapper into the venv
#   /usr/lib/systemd/user/familymon-*   capture/classify/heartbeat/dashboard
#   /etc/familymon/{messaging_apps,game_apps}.yaml   parent-editable conffiles
#                                       (dpkg preserves local edits on upgrade)
#   /usr/share/familymon/policy_example.yaml
#   /usr/share/familymon/chrome-extension/   signed lesson-block extension
#                                       (crx + update.xml + id), built here
#
# The venv ties to the system python3 minor version — Depends pins it. Both
# machines track the same Ubuntu release, so this is a non-constraint.
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/bin:/usr/local/bin:/bin:${PATH:-}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# ---- version: <pyproject>+<date>.g<sha>[.dirty] ----------------------------
BASE_VERSION="$(/usr/bin/python3 -c \
    'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
GIT_SHA="$(git rev-parse --short HEAD)"
DIRTY=""
git diff --quiet && git diff --cached --quiet || DIRTY=".dirty"
VERSION="${BASE_VERSION}+$(date +%Y%m%d.%H%M).g${GIT_SHA}${DIRTY}"
PY_MINOR="$(/usr/bin/python3 -c 'import sys; print(f"3.{sys.version_info.minor}")')"
PY_NEXT="$(/usr/bin/python3 -c 'import sys; print(f"3.{sys.version_info.minor+1}")')"

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
echo "building familymon ${VERSION} (python ${PY_MINOR}) in ${STAGING}"

# ---- bundled venv, frozen to uv.lock ---------------------------------------
# --python /usr/bin/python3 forces the SYSTEM interpreter — a uv-managed
# CPython would leave the venv's symlink dangling on the target machine.
# --relocatable rewrites entry-point shebangs so the venv works at
# /opt/familymon/venv even though it was built in a temp dir.
VENV="$STAGING/opt/familymon/venv"
uv venv --python /usr/bin/python3 --relocatable "$VENV"
# --reinstall-package familymon: the project version in pyproject is static,
# so uv would happily reuse a CACHED familymon wheel from an earlier build —
# which shipped stale code under a fresh version stamp once. Always rebuild
# the project itself; dependencies still come from the cache/lock.
UV_PROJECT_ENVIRONMENT="$VENV" uv sync --frozen --no-dev --all-extras --no-editable \
    --reinstall-package familymon

# Sanity: the venv must point at the system interpreter.
TARGET="$(readlink -f "$VENV/bin/python3")"
case "$TARGET" in
    /usr/bin/python"${PY_MINOR#3.}"*|/usr/bin/python3*) ;;
    *) echo "ERROR: venv python resolves to ${TARGET}, not the system python3"; exit 1 ;;
esac

# Sanity: the staged package must be byte-identical to the working tree —
# the only guard against shipping cached/stale code again.
STAGED_PKG="$(ls -d "$VENV"/lib/python3.*/site-packages/familymon)"
if ! diff -r --exclude=__pycache__ familymon "$STAGED_PKG" >/dev/null; then
    echo "ERROR: staged familymon package differs from the working tree:"
    diff -rq --exclude=__pycache__ familymon "$STAGED_PKG" | head
    exit 1
fi
echo "staged package matches working tree"

# ---- Chrome extension (closes already-open blocked tabs) --------------------
# Signed with a per-deployment key kept OUT of the repo (private/ is
# gitignored). The extension id derives from the key: Chrome force-installs
# exactly this build, and a lost key just means a new id on the next policy
# write. The version must increase every build or Chrome keeps the old one.
EXT_KEY="$REPO/private/chrome-extension-key.pem"
[ -f "$EXT_KEY" ] || echo "no $EXT_KEY — generating a new extension signing key"
"$VENV/bin/python3" -m familymon.cli extension build --key "$EXT_KEY" \
    --out "$STAGING/usr/share/familymon/chrome-extension" \
    --version "1.$(date +%y%m).$(date +%-d).$(date +%-H%M)"

# ---- filesystem payload -----------------------------------------------------
install -D -m 0755 deploy/deb/bin/familymonctl "$STAGING/usr/bin/familymonctl"
install -d "$STAGING/usr/lib/systemd/user"
install -m 0644 deploy/deb/systemd-user/*.service deploy/deb/systemd-user/*.timer \
    "$STAGING/usr/lib/systemd/user/"
# System-scope units (root): the enforcement reconciler.
install -d "$STAGING/usr/lib/systemd/system"
install -m 0644 deploy/deb/systemd-system/*.service deploy/deb/systemd-system/*.timer \
    "$STAGING/usr/lib/systemd/system/"
install -D -m 0644 familymon/defaults/messaging_apps.yaml \
    "$STAGING/etc/familymon/messaging_apps.yaml"
install -D -m 0644 familymon/defaults/game_apps.yaml \
    "$STAGING/etc/familymon/game_apps.yaml"
install -D -m 0644 familymon/defaults/schedule_extras.yaml \
    "$STAGING/etc/familymon/schedule_extras.yaml"
install -D -m 0644 familymon/defaults/policy_example.yaml \
    "$STAGING/usr/share/familymon/policy_example.yaml"
# Desktop entry so Plasma treats familymon notifications as a known app
# (popups on by default) instead of "[@other]", which users often mute.
install -D -m 0644 deploy/deb/familymon.desktop \
    "$STAGING/usr/share/applications/familymon.desktop"

# ---- package metadata -------------------------------------------------------
INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGING" | cut -f1)"
install -d "$STAGING/DEBIAN"
cat > "$STAGING/DEBIAN/control" <<EOF
Package: familymon
Version: ${VERSION}
Section: admin
Priority: optional
Architecture: amd64
Installed-Size: ${INSTALLED_SIZE}
Depends: python3 (>= ${PY_MINOR}), python3 (<< ${PY_NEXT}), kde-spectacle, sqlite3, libnotify-bin
Recommends: timekpr-next, tesseract-ocr
Maintainer: $(git config user.name) <$(git config user.email)>
Description: Local-first parental-control monitoring
 Captures screenshots and window state, classifies activity locally,
 surfaces schedule conflicts on a LAN dashboard, and drives timekpr-next
 for time/app enforcement. Bundles its Python environment (frozen to the
 tested uv.lock) under /opt/familymon.
EOF
# Long-running user units (capture loop, dashboard) keep the OLD code in
# memory after an upgrade — the dashboard once ran stale conflict rules for a
# week. Restart them for every logged-in user that has them active; the
# per-tick timer services pick up new code by themselves. Best effort.
cat > "$STAGING/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ] && command -v loginctl >/dev/null 2>&1; then
    for u in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $2}'); do
        systemctl --user -M "$u@" daemon-reload 2>/dev/null || true
        for unit in familymon-capture.service familymon-dashboard.service; do
            if systemctl --user -M "$u@" is-active --quiet "$unit" 2>/dev/null; then
                systemctl --user -M "$u@" restart "$unit" 2>/dev/null \
                    && echo "familymon: restarted $unit for $u" || true
            fi
        done
    done
fi
exit 0
EOF
chmod 0755 "$STAGING/DEBIAN/postinst"
cat > "$STAGING/DEBIAN/conffiles" <<EOF
/etc/familymon/messaging_apps.yaml
/etc/familymon/game_apps.yaml
/etc/familymon/schedule_extras.yaml
EOF

# ---- build + smoke ----------------------------------------------------------
mkdir -p dist
DEB="dist/familymon_${VERSION}_amd64.deb"
dpkg-deb --build --root-owner-group -Zzstd "$STAGING" "$DEB"

echo "---- smoke: CLI runs from the staged venv ----"
"$VENV/bin/python3" -m familymon.cli --help >/dev/null
echo "---- package info ----"
dpkg-deb -I "$DEB"
echo
echo "built: $DEB"
