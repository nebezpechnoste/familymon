#!/usr/bin/env bash
# Nightly: upgrade familymon's locked Python deps to latest, run the full test
# suite against the upgraded set, and commit+push the new uv.lock ONLY on green.
# On red, the lock and the venv roll back — the environment never rots, and no
# untested dependency set ever reaches a deployed machine.
#
# Cron (user, alongside the yt-dlp/mise upgraders):
#   45 20 * * * <repo>/scripts/uv-upgrade.sh
#
# pyproject constraints stay deliberately loose (>=); uv.lock is the tested
# snapshot that `uv sync` reproduces on the target laptop.
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/bin:/usr/local/bin:/bin:${PATH:-}"

# Cron has no SSH agent, and the GitHub key is passphrase-protected — adopt
# the desktop session's systemd-managed agent socket so the push step works.
# If the session is down, push fails gracefully (commit kept, logged) and the
# next run or an interactive `git push` catches the repo up.
AGENT_SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/openssh_agent"
[ -z "${SSH_AUTH_SOCK:-}" ] && [ -S "$AGENT_SOCK" ] && export SSH_AUTH_SOCK="$AGENT_SOCK"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/familymon-deps-up.log"
mkdir -p "$(dirname "$LOG")"
[ -f "$LOG" ] && { tail -n 1000 "$LOG" >"$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"; }
exec >>"$LOG" 2>&1

echo "================ deps up     $(date -Is) ================"
cd "$REPO" || exit 1

# One at a time.
exec 9>"$REPO/.git/uv-upgrade.lock"
flock -n 9 || { echo "another run holds the lock; skipping"; exit 0; }

# Don't manage a lockfile that has local, uncommitted edits — not ours to lose.
if ! git diff --quiet -- uv.lock; then
    echo "uv.lock has uncommitted changes; skipping"
    exit 0
fi

uv lock --upgrade; rc_lock=$?
if [ $rc_lock -ne 0 ]; then
    echo "uv lock --upgrade failed (rc=$rc_lock); restoring"
    git checkout -- uv.lock
    exit 1
fi

if git diff --quiet -- uv.lock; then
    echo "already current"
    exit 0
fi

echo "---------------- lock changed; testing  $(date -Is) ----------------"
git --no-pager diff --stat -- uv.lock
# `uv run` syncs the venv to the upgraded lock before running the suite.
uv run pytest -q; rc_test=$?

if [ $rc_test -eq 0 ]; then
    git commit -m "chore(deps): nightly locked-dependency upgrade (tests green)" -- uv.lock; rc_commit=$?
    rc_push=-
    if [ $rc_commit -eq 0 ] && [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ]; then
        git push origin main; rc_push=$?
        [ $rc_push -ne 0 ] && echo "push failed; commit kept locally"
    fi
    echo "================ done: upgraded (commit=$rc_commit push=$rc_push)  $(date -Is) ================"
else
    echo "tests FAILED on upgraded deps (rc=$rc_test) — rolling back"
    git checkout -- uv.lock
    uv sync   # restore the venv to the last-good lock
    echo "================ done: rolled back  $(date -Is) ================"
    exit 1
fi
