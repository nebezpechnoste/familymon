# Install familymon

Read this when you must install familymon on the monitored laptop (`kid-laptop`), enable its units for the monitored account (`kid`), run first-run checks, reach the dashboard, or create the `kid` account. For configuring policy after install see [configure-from-intent.md](configure-from-intent.md). To prove changes took effect see [verify-and-health.md](verify-and-health.md). Terms are defined in [../concepts.md](../concepts.md). Paths and units are listed in [../reference/files-and-units.md](../reference/files-and-units.md).

Conventions: "the parent" decides policy. Commands marked `root` need sudo. Anything not confirmed in the code is marked UNVERIFIED.

## 1. Disclosure first

Parent decides: familymon is disclosed monitoring. Tell the monitored user what is captured (window metadata, screenshots with messaging windows blacked out, classifications) and what is enforced before installing.

## 2. Prerequisites

| Requirement | Why | Notes |
|---|---|---|
| KDE Plasma 6 on Wayland, in the monitored user's session | Capture enumerates windows through KWin D-Bus scripting and takes screenshots with `spectacle`. The only capture backend is `kwin_wayland`. There is no X11 backend. | Capture must run inside the kid's graphical session. |
| `timekpr-next` | Time and app enforcement backend (`enforce ...`). | The deb only Recommends it. Install it yourself. See below. |
| Google Chrome | Web blocking writes Chrome managed policy at `/etc/opt/chrome/policies/managed/familymon.json` and force-installs a bundled extension. | The path is Chrome's, not Chromium's. How to install Chrome is not defined by the code: use Google's own package and check the OS-recommended method first. |
| Debian-family system with `python3`, `kde-spectacle`, `sqlite3`, `libnotify-bin` | Declared as deb `Depends`. | `python3` must be the minor version the deb was built against (the deb pins `>= 3.N, << 3.N+1`). Check with `apt-cache policy familymon` before installing. |
| `tesseract-ocr` | deb `Recommends`. | Not used by the current heuristic classifier. Optional. |

Install-method rule: before installing any package, check the OS-recommended path and show alternatives. Do not pick silently.

timekpr-next, root:

```bash
sudo apt install timekpr-next
```

Safety (timekpr PlayTime): per-user PlayTime settings take no effect until the GLOBAL PlayTime switches (including the enhanced activity monitor) are enabled in `/etc/timekpr/timekpr.conf`. The exact key names are not documented in the familymon code. Read the comments in that file and the timekpr-next docs. Verify with `familymonctl enforce show kid` after an apply (see [verify-and-health.md](verify-and-health.md)).

## 3. Create the kid's standard account

Do this before installing capture. The account must be a standard, non-admin user.

Why:
- A user in `sudo`/`admin` can stop or edit anything familymon runs as root: the enforce, web-block and chargeback timers, `/etc/familymon/*`, `/etc/opt/chrome/policies/managed/`, `/etc/timekpr/`, and `/var/lib/familymon/`.
- The kid still owns their own user units and `~/.local/state/familymon/`. They can stop `familymon-capture.service` with `systemctl --user`. That is a known limit of the user-scope design. Detection, not prevention: capture silence shows in `familymonctl health` and in heartbeat events (see [verify-and-health.md](verify-and-health.md)).
- Trust-resistant rule: the app-category lookup deliberately ignores `~/.local/share/applications` because the kid can forge it. Game detection keys on the installed Steam library plus a curated list, not on anything the kid controls.
- Root-side jobs read the kid's capture DB strictly read-only.

Generic Debian/Ubuntu command, root. This is OS administration, not a familymon command:

```bash
sudo adduser kid
id kid        # verify: groups must NOT include sudo, adm, admin or wheel
```

Parent decides: the parent keeps a separate admin account. Do not give `kid` a password-less sudo rule. Do not add `kid` to the `timekpr` group (that group may drive timekpr over D-Bus, per the `enforce` help text).

Undo: `sudo deluser kid` (add `--remove-home` only if the parent wants the data deleted).

## 4. Install the package

The package is `familymon`, `amd64`, installed to `/opt/familymon/venv` with the wrapper `/usr/bin/familymonctl`. It enables nothing and creates no accounts.

Option A: a local `.deb` file, root:

```bash
sudo apt install ./familymon_<version>_amd64.deb
```

Option B: the project's local apt repo (`scripts/publish-deb.sh`). It is a flat, UNSIGNED repo at `/srv/familymon-apt` on the target host. `--setup` writes `/etc/apt/sources.list.d/familymon.sources.list` containing `deb [trusted=yes] file:/srv/familymon-apt ./`. Because it is unsigned, only use it on a host the parent controls. Build and publish steps are in [../develop/testing-and-shipping.md](../develop/testing-and-shipping.md). After publishing:

```bash
sudo apt-get update
apt-cache policy familymon     # confirm the candidate version
sudo apt install familymon
```

Verify the install (read-only):

```bash
dpkg -s familymon | head -5
familymonctl --help
ls /usr/share/familymon/ /etc/familymon/
```

Expected under `/etc/familymon/`: `messaging_apps.yaml`, `game_apps.yaml`, `schedule_extras.yaml` (dpkg conffiles: local edits are preserved on upgrade). `/usr/share/familymon/` holds `policy_example.yaml` and `chrome-extension/` (`familymon.crx`, `update.xml`, `id`, `version`). The deb also installs `/usr/share/applications/familymon.desktop`.

Upgrades: the postinst runs `daemon-reload` for each logged-in user and restarts `familymon-capture.service` and `familymon-dashboard.service` if active. It does not touch system-scope units. Lesson: after an upgrade, if behaviour looks stale compare the unit's `ActiveEnterTimestamp` with the package install time (see [verify-and-health.md](verify-and-health.md)).

## 5. State directories

`state_dir()` resolves: `$FAMILYMON_STATE_DIR`, else `/var/lib/familymon` when running as root, else `~/.local/state/familymon`.

- The kid's data lives in `~kid/.local/state/familymon/` (created by their units: `captures.db` mode 0600 in a 0700 dir, `screenshots/`, `schedule.yaml`).
- Root-side enforcement state lives in `/var/lib/familymon/` (`policy.yaml`, `grants.json`, `playtime_charges.json`, and a root audit `captures.db` holding enforce events).
- The deb does not create `/var/lib/familymon` or a `familymon` system user. The code creates it (mode 0700) only when something opens the root audit DB (`enforce grant`, a changing `enforce web-block` run, a charging `enforce chargeback` run, or a root `health`). `plan`, `apply` and the example copy below need it to exist first, so create it yourself, root:

```bash
sudo install -d -m 0700 /var/lib/familymon
sudo install -m 0600 /usr/share/familymon/policy_example.yaml /var/lib/familymon/policy.yaml
```

Then edit `policy.yaml` following [configure-from-intent.md](configure-from-intent.md). Key reference: [../reference/config.md](../reference/config.md). Without a `policy.yaml`, `enforce plan`, `apply`, `revoke` and `grant --until` fail with a message pointing at the example file, and `enforce web-block` and `chargeback` fail with "no policy at <path>". `enforce show` and `enforce grants` do not read it.

Safety: the shipped example has `games.enabled`, `games.charge_browser_games` and `web_block.enabled` all set to `true`, and `user: kid`. Review it before enabling the system timers, since copying it unchanged turns web blocking and browser-game chargeback on.

## 6. Enable units for the monitored user

No unit is enabled by default. Units are in two scopes.

User scope, run as `kid` inside their session (capture needs `graphical-session.target`):

```bash
systemctl --user enable --now familymon-capture.service
systemctl --user enable --now familymon-classify.timer
systemctl --user enable --now familymon-heartbeat.timer
systemctl --user enable --now familymon-notify.timer
```

The enable commands for the classify and heartbeat timers follow the pattern in the unit comments. Only the capture, notify and system-scope timer enable commands are quoted verbatim in the unit files. The postinst addresses a user's manager with `systemctl --user -M kid@ ...`, so a root shell can use that form while `kid` is logged in. Enabling that way is UNVERIFIED. If `kid` is not logged in, their user manager is not running and the commands fail: do it in their session instead.

Safety: this is the kid's session. Do these steps with the parent present, from the kid's account, not by driving their desktop remotely. Never take a raw screenshot of their session to confirm (see section 9).

Timers use `OnActiveSec`, not `OnBootSec`. A timer with `OnBootSec` never armed when the user logged in long after boot, which left hours of captures unclassified. If you see "unknown" rows, run `familymonctl classify` (and `--redo` after a policy change) as `kid`.

System scope (root). Enable only the ones the parent wants, after a dry run:

| Unit | Does | Preview first |
|---|---|---|
| `familymon-enforce.timer` | `enforce apply` hourly (first run 5 min after boot, plus up to 5 min random delay) | `sudo familymonctl enforce plan` |
| `familymon-chargeback.timer` | `enforce chargeback` every 5 min (first run 3 min after the timer starts), needs `games.enabled` and `games.charge_browser_games` | `sudo familymonctl enforce chargeback --dry-run` |
| `familymon-webblock.timer` | `enforce web-block` every minute, needs `web_block.enabled: true` | `sudo familymonctl enforce web-block --dry-run` |

```bash
sudo systemctl enable --now familymon-enforce.timer
sudo systemctl enable --now familymon-chargeback.timer
sudo systemctl enable --now familymon-webblock.timer
```

Undo: `sudo systemctl disable --now <timer>`. Disabling the enforce timer does not revert what was already applied to timekpr. Use `familymonctl enforce plan` and `enforce apply` to converge, or change timekpr by hand.

Notes:
- `familymon-enforce.service` exits 3 and shows failed when the apply guard refuses (it would cut an active session). That is a refusal, not a crash. Read [verify-and-health.md](verify-and-health.md).
- Chrome policy only bites on navigation. Already-open tabs are closed by the force-installed extension. A Chrome started with `--disable-extensions` runs without it, so the lesson-time sweep closes it.
- The `--dry-run` of web-block still scans processes read-only. It does not kill.

## 7. Schedule (optional but needed for web-block, warnings, conflicts)

As `kid`, so the file lands in `~kid/.local/state/familymon/schedule.yaml`:

```bash
familymonctl schedule import timetable.json      # --format kingsinterhigh is the only format and the default
familymonctl schedule show
```

`timetable.json` is the saved response of `GET /student/api/timetable/week` from the Kings InterHigh student API, refreshed manually each term. Do not run `schedule import` as root: it would write `/var/lib/familymon/schedule.yaml`, which `enforce web-block` does not read (it reads the policy user's file unless `--schedule` is given). `schedule import` overwrites the existing schedule and has no dry run.

## 8. Dashboard access

The dashboard is a user unit (`familymon-dashboard.service`). It reads the DB of the user it runs as (the default `state_dir()`), so run it as `kid` to see their data. Whether it should instead run for the parent is not settled by the code (UNVERIFIED design intent).

It refuses to start unless both credentials are set. Create `/etc/familymon/dashboard.env`, root (not shipped by the package, not a conffile):

```bash
sudo install -m 0644 /dev/null /etc/familymon/dashboard.env
sudoedit /etc/familymon/dashboard.env
```

```ini
FAMILYMON_DASH_USER=parent
FAMILYMON_DASH_PASS=<long random string>
# optional
# FAMILYMON_DASH_PORT=8765
# FAMILYMON_DASH_BIND=127.0.0.1
```

Safety: the unit comment says mode 0644 is fine for a view-only dashboard (root-owned). A user unit running as `kid` must itself read this file, so `kid` can read the password (inference, not stated in the code). The unit loads it with `EnvironmentFile=-`, so a missing file is not an error by itself; the dashboard then fails on the missing credentials. HTTP Basic auth is the only control and there is no TLS. Parent decides whether that is acceptable. If you use a different mode, confirm the unit still starts.

```bash
systemctl --user enable --now familymon-dashboard.service     # as kid
```

Defaults: bind `127.0.0.1`, port `8765`. Reach it at `http://127.0.0.1:8765/` on the laptop, or via an SSH tunnel such as `ssh -L 8765:127.0.0.1:8765 kid-laptop`. `FAMILYMON_DASH_BIND=0.0.0.0` exposes it to the LAN over plain HTTP. Do that only on a network the parent trusts. The env file is read at process start only: restart the unit after editing.

Routes (all need auth, metadata only, no screenshot route): `GET /`, `GET /d/YYYY-MM-DD`, `GET /api/captures?day=`, `GET /api/stats?day=`. The banner shows the capture heartbeat.

## 9. First-run checks

Run in order. Details of how to read each result are in [verify-and-health.md](verify-and-health.md).

1. Units active, as `kid`: `systemctl --user is-active familymon-capture.service` and `systemctl --user list-timers 'familymon-*'`.
2. Wait about 3 minutes, then run health as `kid`: `familymonctl health`. Expect exit 0, `privacy check: OK`, `db integrity: ok`.
3. `familymonctl heartbeat-once` as `kid`. Expect `heartbeat: ok`.
4. `familymonctl classify`, then health again. `classified 24h` should rise.
5. Notifications: `familymonctl schedule notify-tick --test` as `kid`. Safety: this pops a real notification on their screen, so warn them. Confirm via Plasma's notification history or D-Bus state, not a screenshot. Plasma may only show popups for apps with a desktop entry. familymon ships `/usr/share/applications/familymon.desktop` (`NoDisplay=true`) and sends the `desktop-entry` hint so they pop up.
6. Enforcement, root: `sudo familymonctl enforce plan`, review, then `sudo familymonctl enforce apply`, then `sudo familymonctl enforce show kid` after a few seconds.

Safety (screenshots): never take a raw screenshot of the monitored session for diagnostics. It bypasses messaging-window redaction.

## 10. Uninstall or roll back

```bash
sudo systemctl disable --now familymon-enforce.timer familymon-chargeback.timer familymon-webblock.timer
sudo apt remove familymon
```

There is no prerm or postrm. Disable the user units first as `kid`, or they keep running the old code until logout. Chrome policy file, timekpr settings, `/var/lib/familymon` and the kid's state dir are not removed. Remove `/etc/opt/chrome/policies/managed/familymon.json` and reset timekpr by hand if the parent wants a clean state.
