# Install familymon

Read this when you must install familymon on the monitored laptop (`kid-laptop`), start its units for the monitored account (`kid`), run first-run checks, reach the dashboard, upgrade from a per-user release, or create the `kid` account. For configuring policy after install see [configure-from-intent.md](configure-from-intent.md). To prove changes took effect see [verify-and-health.md](verify-and-health.md). Terms are defined in [../concepts.md](../concepts.md). Paths and units are listed in [../reference/files-and-units.md](../reference/files-and-units.md).

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
- A user in `sudo`/`admin` can stop or edit anything familymon runs as root. That is now everything: the capture supervisor and every timer, `/etc/familymon/*`, `/etc/opt/chrome/policies/managed/`, `/etc/timekpr/`, and `/var/lib/familymon/`.
- A standard user cannot stop, mask or edit any of it, and cannot open `/var/lib/familymon` (root:`familymon` 0750). What the kid can still do is limited to making capture ticks fail, which is detected, not prevented (see section 11).
- Trust-resistant rule: the app-category lookup deliberately ignores `~/.local/share/applications` because the kid can forge it. Game detection keys on the installed Steam library plus a curated list, not on anything the kid controls.

Generic Debian/Ubuntu command, root. This is OS administration, not a familymon command:

```bash
sudo adduser kid
id kid        # verify: groups must NOT include sudo, admin or wheel (adm: see section 11)
```

Parent decides: the parent keeps a separate admin account. Do not give `kid` a password-less sudo rule. Do not add `kid` to the `timekpr` group (that group may drive timekpr over D-Bus, per the `enforce` help text).

Undo: `sudo deluser kid` (add `--remove-home` only if the parent wants the data deleted).

## 4. Install the package

The package is `familymon`, `amd64`, installed to `/opt/familymon/venv` with the wrapper `/usr/bin/familymonctl`. The postinst creates the `familymon` system user and group (it runs the read-only dashboard), enables and starts the units, and moves an older release's per-user state into `/var/lib/familymon`. The package ships system units only.

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

Upgrades: on every `configure` the postinst makes sure the `familymon` account exists, forces `/etc/familymon/dashboard.env` to root:root 0600, makes `/var/lib/familymon` root:`familymon` 0750, retires any old per-user units (section 11), runs `daemon-reload`, enables the capture and dashboard services and the six timers, and restarts the two services so they never keep old code in memory. Lesson: after an upgrade, if behaviour looks stale compare the unit's `ActiveEnterTimestamp` with the package install time (see [verify-and-health.md](verify-and-health.md)).

## 5. State directory

Production state is one directory, `/var/lib/familymon`, owned root:`familymon` 0750. `state_dir()` resolves `$FAMILYMON_STATE_DIR` first (the dashboard unit sets it to `/var/lib/familymon`), else `/var/lib/familymon` when running as root, else `~/.local/state/familymon` (dev runs only).

- Readable by the dashboard's `familymon` account (group): `captures.db` and its journal, and `schedule.yaml` (`schedule import`/`refresh` set the mode; a hand-written file needs `0640 root:familymon`).
- Root only: `screenshots/`, `policy.yaml`, `grants.json`, `playtime_charges.json`, `notified.json`, `warned.json`, `known_games.json`.
- The postinst creates the directory. It does not create `policy.yaml`. The monitored account is the `user:` key in `policy.yaml` (or the `FAMILYMON_USER` environment variable).

Create the policy from the example, root:

```bash
sudo install -m 0600 /usr/share/familymon/policy_example.yaml /var/lib/familymon/policy.yaml
```

Then edit `policy.yaml` following [configure-from-intent.md](configure-from-intent.md). Key reference: [../reference/config.md](../reference/config.md). Without a `policy.yaml`, `enforce plan`, `apply`, `revoke` and `grant --until` fail with a message pointing at the example file, and `enforce web-block` and `chargeback` fail with "no policy at <path>". `enforce show` and `enforce grants` do not read it.

Safety: the shipped example has `games.enabled`, `games.charge_browser_games` and `web_block.enabled` all set to `true`, and `user: kid`. Review it before the units start acting on it, since copying it unchanged turns web blocking and browser-game chargeback on.

## 6. Units

All units are system units, root, in `/usr/lib/systemd/system`. The package enables them, so on a fresh install you only have to supply the policy. Units that need configuration skip themselves until it exists: the capture, notify, enforce, chargeback and webblock units carry `ConditionPathExists=/var/lib/familymon/policy.yaml`, and the dashboard waits for `/etc/familymon/dashboard.env`. A condition-skipped service does not retry, so once `policy.yaml` exists, start capture yourself:

```bash
sudo systemctl restart familymon-capture.service
```

| Unit | Does | Preview first |
|---|---|---|
| `familymon-capture.service` | `capture-supervise`: follows the kid's graphical session via logind and records one tick a minute | none (read-only observation) |
| `familymon-classify.timer` | `classify`, 2 min after boot then every 5 min | none |
| `familymon-heartbeat.timer` | `heartbeat-once`, every minute | none |
| `familymon-notify.timer` | `schedule notify-tick`, every minute at :05 | `sudo familymonctl schedule notify-tick --test` |
| `familymon-enforce.timer` | `enforce apply` hourly (first run 5 min after boot, plus up to 5 min random delay) | `sudo familymonctl enforce plan` |
| `familymon-chargeback.timer` | `enforce chargeback` every 5 min (first run 3 min after the timer starts), needs `games.enabled` and `games.charge_browser_games` | `sudo familymonctl enforce chargeback --dry-run` |
| `familymon-webblock.timer` | `enforce web-block` every minute, needs `web_block.enabled: true` | `sudo familymonctl enforce web-block --dry-run` |
| `familymon-dashboard.service` | the read-only dashboard, as the `familymon` user | none |

Because the enforce, chargeback and webblock timers are enabled as soon as `policy.yaml` exists, preview the policy with `plan` and the `--dry-run` forms before you save it, or keep the relevant sections off (`games.enabled: false`, `web_block.enabled: false`) until the parent has agreed.

Check them (section 9). Undo: `sudo systemctl disable --now <unit>`. Disabling the capture, heartbeat or enforce units stops monitoring or enforcement for the kid, so the parent decides. Disabling the enforce timer does not revert what was already applied to timekpr. Use `familymonctl enforce plan` and `enforce apply` to converge, or change timekpr by hand. Note that `apt` re-enables everything on the next upgrade.

Notes:
- `familymon-enforce.service` exits 3 and shows failed when the apply guard refuses (it would cut an active session). That is a refusal, not a crash. Read [verify-and-health.md](verify-and-health.md).
- Chrome policy only bites on navigation. Already-open tabs are closed by the force-installed extension. A Chrome started with `--disable-extensions` runs without it, so the lesson-time sweep closes it.
- The `--dry-run` of web-block still scans processes read-only. It does not kill.
- Nothing runs inside the kid's session between capture ticks. Each tick starts a short-lived helper as `kid` from root-owned code. Do not take a raw screenshot of their session to confirm anything (see section 9).
- Dev only: `deploy/systemd/user/` holds user units for running `familymonctl capture-run` inside your own session on a developer machine. They are not packaged; do not use them for installs.

## 7. Schedule (optional but needed for web-block, warnings, conflicts)

As root. The schedule lives in `/var/lib/familymon/schedule.yaml`, which every consumer reads:

```bash
sudo familymonctl schedule import timetable.json      # --format kingsinterhigh is the only format and the default
sudo familymonctl schedule show
```

`timetable.json` is the saved response of `GET /student/api/timetable/week` from the Kings InterHigh student API, refreshed manually each term. `schedule import` overwrites the existing schedule and has no dry run: copy the old file first.

## 8. Dashboard access

The dashboard is a system unit (`familymon-dashboard.service`) running as the unprivileged `familymon` user with `FAMILYMON_STATE_DIR=/var/lib/familymon`. It opens the capture database read-only, can read screenshots and the schedule through group membership, and cannot read `policy.yaml` or anything else root-only. The unit is sandboxed (`ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges=yes`, private `/tmp` and devices).

It refuses to start unless both credentials are set. Create `/etc/familymon/dashboard.env`, root (not shipped by the package, not a conffile). The mode is 0600 root:root: systemd reads it as root before dropping privileges, so neither the dashboard process nor the kid can read the password. The postinst forces that mode if the file already exists.

```bash
sudo install -m 0600 -o root -g root /dev/null /etc/familymon/dashboard.env
sudoedit /etc/familymon/dashboard.env
```

```ini
FAMILYMON_DASH_USER=parent
FAMILYMON_DASH_PASS=<long random string>
# optional
# FAMILYMON_DASH_PORT=8765
# FAMILYMON_DASH_BIND=127.0.0.1
```

HTTP Basic auth is the only control and there is no TLS. Parent decides whether that is acceptable.

```bash
sudo systemctl restart familymon-dashboard.service
```

(The unit is enabled by the package but skipped until the env file exists, so start it once after creating the file.) Defaults: bind `127.0.0.1`, port `8765`. Reach it at `http://127.0.0.1:8765/` on the laptop, or via an SSH tunnel such as `ssh -L 8765:127.0.0.1:8765 kid-laptop`. `FAMILYMON_DASH_BIND=0.0.0.0` exposes it to the LAN over plain HTTP. Do that only on a network the parent trusts. The env file is read at process start only: restart the unit after editing.

Routes (all need auth, metadata only, no screenshot route): `GET /`, `GET /d/YYYY-MM-DD`, `GET /api/captures?day=`, `GET /api/stats?day=`. The banner shows the capture heartbeat (see [verify-and-health.md](verify-and-health.md)) and, when there were problems that day, a red line counting failed captures and rejected forged window-list pushes.

## 9. First-run checks

Run in order. Details of how to read each result are in [verify-and-health.md](verify-and-health.md).

1. Units active: `systemctl status familymon-capture.service` (active, "running") and `systemctl list-timers 'familymon-*'` (every timer has a NEXT time). The kid must be logged in for captures to appear.
2. Wait about 3 minutes, then run `sudo familymonctl health`. Expect exit 0, `privacy check: OK`, `db integrity: ok`.
3. `sudo familymonctl heartbeat-once`. Expect `heartbeat: ok` (or `heartbeat: idle` if the kid is not logged in).
4. `sudo familymonctl classify`, then health again. `classified 24h` should rise.
5. Failures: `sudo familymonctl events` should list no `capture_failed` or `capture_tamper` rows for today.
6. Notifications: `sudo familymonctl schedule notify-tick --test` (prints "no session to notify" if the kid is not logged in). Safety: this pops a real notification on their screen, so warn them. Confirm via Plasma's notification history or D-Bus state, not a screenshot. Plasma may only show popups for apps with a desktop entry. familymon ships `/usr/share/applications/familymon.desktop` (`NoDisplay=true`) and sends the `desktop-entry` hint so they pop up.
7. Enforcement: `sudo familymonctl enforce plan`, review, then `sudo familymonctl enforce apply`, then `sudo familymonctl enforce show kid` after a few seconds.

Safety (screenshots): never take a raw screenshot of the monitored session for diagnostics. It bypasses messaging-window redaction.

## 10. Uninstall or roll back

```bash
sudo apt remove familymon
```

The prerm disables and stops the capture and dashboard services and all six timers on `remove`. There is no postrm. The Chrome policy file, timekpr settings, the `familymon` system account and `/var/lib/familymon` (captures, screenshots, policy, grants) are not removed. Remove `/etc/opt/chrome/policies/managed/familymon.json` and reset timekpr by hand if the parent wants a clean state, and delete `/var/lib/familymon` only if the parent wants the data gone.

## 11. Upgrading from a per-user release

Earlier releases ran capture, classify, heartbeat, notify and the dashboard as the kid's user units and kept their data in `~kid/.local/state/familymon/`. The kid owned all of that and could stop it. Installing the current package moves everything to root. The postinst does this automatically:

1. Stops the old units in every running user manager and kills any old `familymon.cli capture-run|dashboard` process.
2. Deletes `familymon-*` symlinks (enable and mask links) under every home's `~/.config/systemd/user`, without following symlinks.
3. When `/var/lib/familymon/policy.yaml` exists, runs `familymonctl migrate-state`: copies the kid's capture DB (via the SQLite backup API, rewriting screenshot paths), moves the screenshots, copies `schedule.yaml`, `notified.json` and `warned.json` if root has none, and moves the leftover directory to `/var/lib/familymon/migrated/<user>-<timestamp>/` as a root-only backup. Nothing is deleted. It is idempotent and resumable; the marker is `/var/lib/familymon/.migrated-from-<user>`. If it fails, re-run `sudo familymonctl migrate-state` (add `--user kid` if there is no `policy.yaml`).
4. Enables and starts the new units.

What you do afterwards (the parent decides the second item):

1. **Rotate the dashboard password.** Earlier releases suggested a world-readable `/etc/familymon/dashboard.env` (mode 0644), so treat the old password as known to the kid. Edit the file, set a new `FAMILYMON_DASH_PASS`, then `sudo systemctl restart familymon-dashboard.service`.
2. **Recommended: remove the kid from the `adm` group** if they are in it (`id kid`): `sudo gpasswd -d kid adm`. `adm` lets them read system logs, which include familymon's journal. Undo: `sudo gpasswd -a kid adm`.
3. Verify: `systemctl status familymon-capture.service`, `systemctl list-timers 'familymon-*'`, `sudo familymonctl health`, `sudo familymonctl events`. Once satisfied and the parent agrees, the backup under `/var/lib/familymon/migrated/` can be deleted.

### What the kid can still do

Detected, not prevented (full threat model: [../develop/tamper-resistance.md](../develop/tamper-resistance.md)):

- Log out, restart KWin, kill each capture helper, or squat the `org.familymon.WindowSink` bus name. Capture ticks then fail: `capture_failed` events appear, and the heartbeat goes stale or dead while the session is open (the dashboard banner turns red).
- Turn off familymon's notifications in Plasma settings. Notifications are advisory; monitoring and enforcement continue.
- Log in on a text console. There is no KWin session there, so nothing is captured.

Anything that needs root or physical access (boot another OS, a root shell) is out of scope.
