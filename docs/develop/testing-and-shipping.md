# Testing and shipping

Read this when you need to run the tests, change dependencies, build the `.deb`, publish it to an apt repo, understand CI, or handle the pre-push leak guard and the Chrome extension signing key. For module layout see [architecture.md](architecture.md). For installing the built package see [../operate/install.md](../operate/install.md).

All commands here run on the dev machine, not on `kid-laptop`, and need no root unless stated.

## Running tests

Python (uses the locked environment):

```
uv run pytest -q
```

CI uses `uv run --python 3.14 --all-extras --locked pytest -q`. The extension rules are tested with node (`tests/js/rules.test.mjs`) and are run from pytest by `tests/test_extension_js.py`, which is skipped when `node` is not installed. It passes files, not a directory, to `node --test` because older node treats a directory as a module. CI installs `tesseract-ocr` and node 22.

Test files live in `tests/` and mirror modules (for example `test_heuristics.py`, `test_webblock.py`, `test_recovery.py`).

## Test rules

Safety: tests and dev runs must never touch real processes or real user state.

- Destructive paths (process sweeps, kills) are tested only against processes the test itself spawned. A sweep test that used the default browser list against the dev uid once killed the developer's browser.
- Process kills require root and refuse uid 0 by design. Do not weaken those guards to make a test pass.
- Do not run `enforce`, `capture-once`, `capture-run`, `notify-tick` or timekpr commands from tests. Use temporary state directories (`FAMILYMON_STATE_DIR`, `FAMILYMON_CONFIG_DIR`).
- Never take a raw screenshot of the monitored user's session for diagnostics. It bypasses messaging redaction. Use the desktop's notification history, D-Bus state, or familymon's own redacting capture.
- Root-side code opens the kid's DB read-only (`mode=ro`). Keep tests consistent with that.

Gate on real exit codes. Never chain a test run through a pipe into a build or publish:

```
# wrong: the pipe hides pytest's exit code
uv run pytest -q | tail && scripts/build-deb.sh

# right: separate steps, check each result
uv run pytest -q
echo $?
```

Run verify and ship as separate calls.

## Dependency policy and nightly upgrader

Environments are kept continuously upgraded. `uv.lock` is a tested snapshot, never frozen.

`scripts/uv-upgrade.sh` (intended for user cron, example `45 20 * * *`):

1. Adopts `$XDG_RUNTIME_DIR/openssh_agent` if `SSH_AUTH_SOCK` is unset.
2. Takes `flock -n` on `.git/uv-upgrade.lock`; skips if held.
3. Skips if `uv.lock` has uncommitted changes.
4. Runs `uv lock --upgrade`; on failure restores the lock, exits 1.
5. If the lock is unchanged, exits 0 ("already current").
6. Runs `uv run pytest -q`. On green, commits `chore(deps): nightly locked-dependency upgrade (tests green)` and pushes `origin main` only if on `main`.
7. On red, runs `git checkout -- uv.lock` and `uv sync`, exits 1.

Log: `${XDG_STATE_HOME:-~/.local/state}/familymon-deps-up.log`, trimmed to 1000 lines. It runs the test suite, so it also spawns test processes; the test rules apply.

## Building the deb

```
scripts/build-deb.sh
```

Writes `dist/familymon_<VERSION>_amd64.deb`. Changes the local tree only (`dist/`, a temp staging dir, and `private/chrome-extension-key.pem` if it must be generated).

- Version: `<pyproject version>+<YYYYMMDD.HHMM>.g<short sha>[.dirty]`.
- Bundles a relocatable venv at `/opt/familymon/venv` (`uv venv --relocatable`, `uv sync --frozen --no-dev --all-extras --no-editable`). `/usr/bin/familymonctl` is a wrapper around `/opt/familymon/venv/bin/python3 -m familymon.cli`.
- Checks: the venv python resolves to system python3; the staged `familymon` package is byte-identical to the working tree (else exit 1). A smoke test runs `familymon.cli --help` from the staged venv.
- Depends: `python3` (matching the build host's minor version), `kde-spectacle`, `sqlite3`, `libnotify-bin`, `adduser`, `util-linux` (`runuser`), `procps` (`pkill`), `findutils`. Recommends: `timekpr-next`, `tesseract-ocr`.
- Conffiles: `/etc/familymon/{messaging_apps,game_apps,schedule_extras}.yaml`.
- Installs the system units from `deploy/deb/systemd-system/` to `/usr/lib/systemd/system/` (the deb ships no user units; `deploy/systemd/user/` is dev-only), and `familymon.desktop` (`NoDisplay=true`, used as the notification `desktop-entry` hint).
- Builds the Chrome extension into `/usr/share/familymon/chrome-extension` with version `1.<yymm>.<d>.<Hmm>`. Each build must have a higher version or Chrome keeps the old one.
- postinst: on `configure` only (always exits 0): creates the `familymon` system user and group; forces `/etc/familymon/dashboard.env` to root:root 0600 if present; makes `/var/lib/familymon` root:`familymon` 0750; stops the old per-user units in every running user manager and `pkill`s old `familymon.cli capture-run|dashboard` processes; deletes `familymon-*` symlinks (enable and mask links) under every home's `~/.config/systemd/user` with `find -delete` (no symlink following); runs `familymonctl migrate-state` when `policy.yaml` exists; `daemon-reload`; enables the capture and dashboard services and all seven timers (classify, heartbeat, notify, enforce, chargeback, webblock, retention); restarts the two services and starts the timers.
- prerm: on `remove` only, `systemctl disable --now` for the two services and seven timers. No postrm or preinst exists.
- Compression: `dpkg-deb --build --root-owner-group -Zzstd`.

Parent decides: the package enables every unit itself. Units that need configuration skip themselves via `ConditionPathExists` until `policy.yaml` (or `dashboard.env`) exists, so a fresh install does nothing until the parent's policy is in place. Changes to the postinst touch a kid's laptop on every unattended upgrade: test them on a throwaway VM or container, never on a real family's machine.

Verify: `dpkg-deb -I dist/familymon_*.deb` and `dpkg-deb -c dist/familymon_*.deb`. Undo: delete the file in `dist/`.

## Publishing to an apt repo

`scripts/publish-deb.sh` builds a flat repo and pushes it over ssh. Requires `FAMILYMON_APT_HOST=user@host` (or `private/deploy.conf`, which it sources if present); aborts if unset.

One-time setup (touches the remote, needs sudo there):

```
FAMILYMON_APT_HOST=user@host scripts/publish-deb.sh --setup
```

Creates `/srv/familymon-apt` owned by the login user and writes `/etc/apt/sources.list.d/familymon.sources.list` with `deb [trusted=yes] file:/srv/familymon-apt ./`.

Publish:

```
FAMILYMON_APT_HOST=user@host scripts/publish-deb.sh
```

Steps: pick the newest `dist/familymon_*_amd64.deb` (exit 1 if none), copy to `dist/repo`, keep the 3 newest, run `dpkg-scanpackages --multiversion`, gzip `Packages`, write `Release`, `rsync -az --delete` to the host, then `apt-get update` remotely and show `apt-cache policy familymon`.

Safety:

- The repo is unsigned and relies on `[trusted=yes]`. Treat the host as trusted.
- The script comment assumes the target's unattended-upgrades allows `origin=*`, so new versions install unattended. The repo does not configure that.
- After an upgrade, long-running units may keep old code. The postinst restarts the capture and dashboard system units. If behaviour looks stale, compare the unit's `ActiveEnterTimestamp` with the package install time.
- Run the tests (separate step) and `familymonctl health` on the target before shipping each iteration; keep capture running.

## CI

`.github/workflows/tests.yml`: runs on push to `main` and on every pull request, on `ubuntu-latest`, with a read-only `GITHUB_TOKEN`. Steps: `actions/checkout`, `astral-sh/setup-uv`, `sudo apt-get update && sudo apt-get install -y --no-install-recommends tesseract-ocr`, `actions/setup-node` (node 22), then `uv run --python 3.14 --all-extras --locked pytest -q`. The action versions are whatever the file pins; Dependabot bumps them (below). Note for the maintainer: a `gh` token without the `workflow` scope cannot merge a PR that edits a workflow file; merge such a PR with git over SSH (`git fetch origin pull/N/head`, merge, push) or run `gh auth refresh -s workflow` once.

GitHub-side checks on the public repository, all free for a public repo (set up 2026-10-19):

- **Dependabot alerts and security updates**: an alert and a PR when a locked Python or npm dependency has a published vulnerability. The nightly upgrader usually gets there first; such a PR can be closed. `.github/dependabot.yml` also opens one grouped PR a month for new major versions of the workflow's Actions. Python packages are deliberately not managed by Dependabot (see the dependency policy above).
- **CodeQL** default setup for Python, JavaScript and Actions: scans every push to `main`, every PR and weekly. Findings land in the Security tab and never fail a build.
- **Ruleset "protect main"**: blocks force-pushes and deletion of `main` for everyone, maintainer included. A history rewrite needs the ruleset disabled first, then re-enabled.
- **CodeRabbit** (`.coderabbit.yaml`): reviews pull requests only, skips Dependabot's PRs, and carries the project's privacy and safety rules as path instructions. Direct pushes to `main` are not reviewed, so open a PR for anything non-trivial. The app is installed on this repository only, never on the maintainer's private repository.

## Pre-push leak guard (maintainer)

`.githooks/pre-push` blocks pushes that would publish private strings. Enable once per clone:

```
git config core.hooksPath .githooks
```

- Reads an extended-regex denylist, one pattern per line, `#` comments, case-insensitive. Default `private/publish-denylist.txt`; override with `$FAMILYMON_DENYLIST`.
- Fails closed: a missing denylist makes the push exit 1.
- Scans commit messages, author and committer identity, and added lines (not removed lines). New branch: everything not on any remote. Existing branch: `remote..local`.
- Prints up to 20 hits and exits 1 on any match.
- Read-only (runs `git log`). `private/` is gitignored; keep the denylist there. Never commit its contents or put real names, hostnames or addresses in public files.

## Extension signing key

- Key: `private/chrome-extension-key.pem`, RSA 2048 via `openssl genrsa`, chmod 0600, gitignored. Never commit or publish it.
- `build-deb.sh` generates it if missing. Build manually:

```
familymonctl extension build --key private/chrome-extension-key.pem --out <dir> [--version V]
```

  Writes `familymon.crx`, `update.xml`, `id`, `version`. Needs `openssl`, not root.
- The extension id derives from the public key (first 16 bytes of the SHA-256 of the DER public key, mapped to a-p). A lost key only changes the extension id; the build generates a new key and the policy picks up the new id.
- Chrome fetches updates via a `file://` update manifest, and the policy force-installs `<id>;file://<dir>/update.xml`.
- Back up the key privately if you want a stable id across rebuilds.

## Unsure or not verified

- Whether the deb or any script creates a `familymon` system user or `/var/lib/familymon`.
- None other. The `crx.py` build facts above (key generation, id derivation, `file://` update manifest) were checked against the code.
