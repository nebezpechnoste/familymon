# Files, units, database and events

Read this when you need to find where a piece of familymon state lives (a file, a directory, a systemd unit, a database column or an event row), what permissions it has, what writes it, or how to inspect it safely. It is a lookup reference. For command syntax see [cli.md](cli.md); for policy and config keys see [config.md](config.md); for concepts see [../concepts.md](../concepts.md). Install steps: [../operate/install.md](../operate/install.md). Health checks: [../operate/verify-and-health.md](../operate/verify-and-health.md). Failures: [../operate/troubleshoot.md](../operate/troubleshoot.md).

Placeholders used throughout: the monitored account is `kid`, the laptop is `kid-laptop`, the parent is "the parent".

**Safety.** The capture database and screenshots hold the kid's activity. Read them only as the parent's agent, never copy them into public places, and never take raw screenshots of the kid's session for diagnostics (that bypasses messaging redaction). The root chargeback job opens the kid's database read-only (SQLite URI `mode=ro`).

## 1. State directory resolution

`state_dir()` (`familymon/config.py`) resolves in this order:

1. `$FAMILYMON_STATE_DIR`, if set.
2. `/var/lib/familymon`, if the effective uid is 0.
3. `${XDG_STATE_HOME:-~/.local/state}/familymon`.

`user_state_dir(user)` is `<home of user>/.local/state/familymon`. Root-side reconcilers use it to read the kid's data. It ignores `FAMILYMON_STATE_DIR` and `XDG_STATE_HOME`.

A command run as your own login user (dev or inspection) therefore sees your own `~/.local/state/familymon`, not the kid's and not `/var/lib/familymon`. `familymonctl status` prints the resolved state dir, DB path and screenshots dir (read-only).

## 2. Files and directories

### 2.1 Per-user state: `~kid/.local/state/familymon/`

Written by the kid's user units (capture, classify, heartbeat, notify).

| Path | Written by | Contents | Permissions |
|---|---|---|---|
| `captures.db` (plus `-wal`, `-shm`) | `capture-run`, `capture-once`, `classify`, `heartbeat-once` | SQLite DB, see section 4 | file 0600 in a 0700 directory, re-asserted on every open |
| `captures.db.corrupt-<YYYYmmdd-HHMMSS>` | DB self-recovery | the damaged original, kept, never deleted; `-journal`, `-wal`, `-shm` siblings are renamed with it | as moved |
| `screenshots/<YYYY-MM-DD>/<capture_id>.png` | capture loop | one PNG per persisted capture; the DB stores only the path | root and day directories chmod 0700 on every write; the PNG uses the umask default but sits inside the 0700 directories |
| `schedule.yaml` | `schedule import`, `schedule refresh` | keys `timezone`, `blocks`, optional `breaks` | parent directory created 0700 |
| `notified.json` | `schedule notify-tick` | `{"<ISO date>": ["HH:MM\|subject", ...]}`; overwritten with only today's key; makes block-start notifications fire at most once per day | default |
| `warned.json` | `schedule notify-tick` | `{"<key>": "<ISO timestamp>"}`; entries from other days pruned; drives the live-warning cooldown | default |

A screenshot is saved only when image bytes exist and the capture is not `same_as_prev`. Screenshots are skipped for suspended sessions and for messaging windows that could not be redacted (see section 4.1).

**No retention exists.** No code prunes `capture`, `classification` or `event` rows or screenshot files. Disk use grows until the parent cleans up manually. `familymonctl health` reports PNG count and size. Parent decides retention; if you delete screenshots, leave the DB rows (the DB stores paths only, and the dashboard reports `screenshot_on_disk` as a boolean).

### 2.2 Root state: `/var/lib/familymon/`

Used by `enforce ...` commands run as root (and by the system timers).

| Path | Written by | Contents |
|---|---|---|
| `policy.yaml` | the parent (hand-edited) | enforcement policy, see [config.md](config.md). If missing, `enforce` fails and points to `/usr/share/familymon/policy_example.yaml`. |
| `grants.json` | `enforce grant`, `enforce revoke` | `{"grants":[{user, day, start, end, created_at, note}]}`. Directory created 0700; file uses the default mode. Expired or other-day grants are pruned when active grants are evaluated (`GrantStore.active`) and when one is added; `enforce revoke` rewrites the file without the user's grants for today. |
| `playtime_charges.json` | `enforce chargeback` | `{user: {day, last_capture_id, charged_seconds_today}}`. `charged_seconds_today` resets on a new day; the `last_capture_id` watermark is kept across days. |
| `captures.db` | `enforce` commands | the root audit DB; holds the `Event` rows written by enforcement (section 5). Not the kid's capture data. |

`enforce chargeback` reads the kid's capture DB via `user_state_dir(user)`, strictly read-only (`mode=ro`); it is the only root job found in the code that reads that DB. `enforce web-block` reads the kid's `schedule.yaml` from the same directory (unless `--schedule FILE` is given).

### 2.3 Configuration lookup

`config_file(name)` resolves in this order:

1. `$FAMILYMON_CONFIG_DIR/<name>`
2. `/etc/familymon/<name>`
3. The bundled default in the package (`familymon/defaults/`)

Bundled defaults: `messaging_apps.yaml`, `game_apps.yaml`, `schedule_extras.yaml`, `domains_starter.yaml`, `title_brands.yaml`, `policy_example.yaml`. Only the first three are installed to `/etc/familymon/` by the deb; the others are used from the bundle unless you place an override in `/etc/familymon/` or `$FAMILYMON_CONFIG_DIR`.

### 2.4 Package payload (deb)

| Path | Contents | Mode |
|---|---|---|
| `/opt/familymon/venv` | bundled Python venv | n/a |
| `/usr/bin/familymonctl` | wrapper that runs `/opt/familymon/venv/bin/python3 -m familymon.cli` | 0755 |
| `/etc/familymon/messaging_apps.yaml` | conffile: window classes and title patterns that trigger redaction | 0644 |
| `/etc/familymon/game_apps.yaml` | conffile: curated game window classes and binaries | 0644 |
| `/etc/familymon/schedule_extras.yaml` | conffile: schedule exclusions, custom blocks, breaks | 0644 |
| `/usr/share/familymon/policy_example.yaml` | example policy | 0644 |
| `/usr/share/familymon/chrome-extension/{familymon.crx,update.xml,id,version}` | built lesson-block extension | 0644 |
| `/usr/share/applications/familymon.desktop` | `NoDisplay=true`; its name is passed as the `desktop-entry` hint so Plasma shows familymon notifications as popups | 0644 |
| `/usr/lib/systemd/user/familymon-*` | user units (section 3) | 0644 |
| `/usr/lib/systemd/system/familymon-*` | system units (section 3) | 0644 |

Not shipped: `/etc/familymon/dashboard.env` (optional, you create it; section 3.3). The package does not create a `familymon` system user or `/var/lib/familymon` (no script does; the code mentions a system user only in a comment). Root creates `/var/lib/familymon` implicitly when `enforce` runs as root; verify it exists before relying on it.

The deb has no prerm or postrm. The postinst runs only on `configure`: for each logged-in user it runs `systemctl --user -M <user>@ daemon-reload`, then restarts `familymon-capture.service` and `familymon-dashboard.service` if active (best effort, always exits 0). It does not enable anything and does not touch system-scope units.

### 2.5 Other paths

| Path | Purpose |
|---|---|
| `/etc/opt/chrome/policies/managed/familymon.json` | Chrome managed policy, written by `enforce web-block` as root (mode 0644). Written atomically via `/etc/opt/chrome/policies/.familymon.json.tmp` then rename. Deleted when `web_block` is disabled in `policy.yaml`. |
| `/etc/familymon/dashboard.env` | optional env file with `FAMILYMON_DASH_USER` and `FAMILYMON_DASH_PASS`; the unit's comment recommends root-owned, mode 0644 |
| `familymon_winlist_*.js` (temp) | KWin enumeration script, deleted on close |
| `familymon_shot_*.png` (temp) | per-tick spectacle output, deleted after reading |
| `${XDG_STATE_HOME:-~/.local/state}/familymon-deps-up.log` | dev-machine nightly dependency-upgrade log, trimmed to last 1000 lines |
| `<repo>/.git/uv-upgrade.lock` | dev-machine lock for `scripts/uv-upgrade.sh` |
| `dist/familymon_<VERSION>_amd64.deb`, `dist/repo` | deb build output and local apt repo staging (dev machine) |
| `private/chrome-extension-key.pem`, `private/deploy.conf`, `private/publish-denylist.txt` | extension signing key (0600), deploy settings, pre-push denylist. `private/` is gitignored. |

Safety: the extension signing key is generated per deployment (`openssl genrsa 2048`). A lost key only changes the extension id. Do not publish `private/` contents.

### 2.6 Environment variables

| Variable | Effect |
|---|---|
| `FAMILYMON_STATE_DIR` | overrides the state directory |
| `FAMILYMON_CONFIG_DIR` | first location searched for config YAML |
| `FAMILYMON_INTERVAL` | capture interval in seconds, default 60 |
| `FAMILYMON_DASH_USER`, `FAMILYMON_DASH_PASS` | required dashboard credentials (HTTP Basic, realm `familymon`); the app refuses to start if either is unset or empty |
| `FAMILYMON_DASH_BIND`, `FAMILYMON_DASH_PORT` | dashboard bind (default `127.0.0.1`) and port (default `8765`); `--bind` and `--port` override |
| `FAMILYMON_APT_HOST` | dev-machine publish target for `scripts/publish-deb.sh` |
| `FAMILYMON_DENYLIST` | pre-push hook denylist path |

## 3. systemd units

The deb installs these units but **enables none of them**. Enable by hand. Units hard-coded to a developer checkout under `deploy/systemd/user/` are dev-only and are not packaged; do not use them for installs.

### 3.1 Unit table

| Unit | Scope | Schedule / trigger | Runs |
|---|---|---|---|
| `familymon-capture.service` | user (`kid`) | `WantedBy=graphical-session.target`, `After`/`PartOf` the same; `Restart=on-failure`, `RestartSec=10`, `Nice=10` | `/usr/bin/familymonctl capture-run` |
| `familymon-classify.timer` | user | `OnActiveSec=2min`, `OnUnitActiveSec=5min`, `Persistent=true` | triggers `familymon-classify.service` |
| `familymon-classify.service` | user | oneshot, `Nice=15` | `familymonctl classify` |
| `familymon-heartbeat.timer` | user | `OnActiveSec=1min`, `OnUnitActiveSec=1min`, `Persistent=true` | triggers `familymon-heartbeat.service` |
| `familymon-heartbeat.service` | user | oneshot; non-zero exit marks the unit failed | `familymonctl heartbeat-once` |
| `familymon-notify.timer` | user | `OnActiveSec=30s`, `OnUnitActiveSec=1min`, `AccuracySec=15s`, `Persistent=false` | triggers `familymon-notify.service` |
| `familymon-notify.service` | user | oneshot; needs the session bus for `notify-send` | `familymonctl schedule notify-tick` |
| `familymon-dashboard.service` | user | `WantedBy=default.target`, `Restart=on-failure`, `RestartSec=10`, `EnvironmentFile=-/etc/familymon/dashboard.env` | `familymonctl dashboard` |
| `familymon-enforce.timer` | system (root) | `OnBootSec=5min`, `OnUnitActiveSec=1h`, `RandomizedDelaySec=5min`, `Persistent=true` | triggers `familymon-enforce.service` |
| `familymon-enforce.service` | system (root) | oneshot, `After=timekpr.service`; exit 3 means the apply guard refused (unit shows failed) | `familymonctl enforce apply` |
| `familymon-chargeback.timer` | system (root) | `OnActiveSec=3min`, `OnUnitActiveSec=5min`, `Persistent=true` | triggers `familymon-chargeback.service` |
| `familymon-chargeback.service` | system (root) | oneshot, `After=timekpr.service` | `familymonctl enforce chargeback` |
| `familymon-webblock.timer` | system (root) | `OnCalendar=*-*-* *:*:00` (every minute), `AccuracySec=5s`, `Persistent=false` | triggers `familymon-webblock.service` |
| `familymon-webblock.service` | system (root) | oneshot | `familymonctl enforce web-block` |

User timers use `OnActiveSec`, not `OnBootSec`. A user timer based on `OnBootSec` never armed when the kid logged in long after boot, and captures stayed unclassified. Unclassified rows show as `unknown`; `familymonctl classify --redo` relabels history (see [cli.md](cli.md)). The system enforce timer keeps `OnBootSec` because system timers do arm at boot.

### 3.2 Enable, inspect, undo

User units (run as `kid`, or with `systemctl --user -M kid@`). Needs no root when run in the kid's own session; the capture unit must run inside the kid's graphical session.

```
systemctl --user enable --now familymon-capture.service
systemctl --user enable --now familymon-classify.timer familymon-heartbeat.timer familymon-notify.timer
systemctl --user status familymon-capture.service
systemctl --user list-timers
```

System units (root):

```
sudo systemctl enable --now familymon-enforce.timer familymon-chargeback.timer familymon-webblock.timer
sudo systemctl list-timers 'familymon-*'
```

Undo: `systemctl [--user] disable --now <unit>`. Safety: disabling the capture, heartbeat or enforce timers stops monitoring or enforcement for the kid. Parent decides; tell the parent first. Enabling `familymon-enforce.timer` applies the policy hourly and will revert manual changes to allowed hours (use `enforce grant`; see [cli.md](cli.md)). Disabling `familymon-webblock.timer` leaves the last-written Chrome policy file in place until `enforce web-block` runs again.

### 3.3 Stale code after upgrade

Long-running user units (capture loop, dashboard) can keep running old code after a package upgrade. The postinst restarts them for logged-in users. If behaviour looks stale, compare the unit's `ActiveEnterTimestamp` with the package install time:

```
systemctl --user show familymon-capture.service -p ActiveEnterTimestamp
dpkg-query -W -f='${Version}\n' familymon
```

### 3.4 Dashboard notes

`familymon-dashboard.service` runs as the user whose DB it reads. It needs `FAMILYMON_DASH_USER` and `FAMILYMON_DASH_PASS` (in `/etc/familymon/dashboard.env` or the environment). Auth is HTTP Basic with no TLS; the only exposure control is the bind address (default `127.0.0.1`). Routes: `GET /`, `GET /d/{YYYY-MM-DD}`, `GET /api/captures?day=`, `GET /api/stats?day=`. It serves metadata only, never image bytes. Whether the dashboard is intended to run for the kid or for the parent is not settled in the code; it reads the DB of the account it runs as.

## 4. Database schema

Engine: SQLAlchemy over SQLite (`familymon/storage/models.py`), tables created with `create_all` on open. Timestamps are timezone-aware for the capture and event tables as declared; health and dashboard code compares local-naive times. Three tables exist: `capture`, `classification`, `event`.

### 4.1 Table `capture`

| Column | Type | Notes |
|---|---|---|
| `id` | int | primary key |
| `captured_at` | datetime (tz) | indexed |
| `active_wm_class` | str(256) | nullable |
| `active_title` | str(512) | nullable |
| `active_pid` | int | nullable |
| `active_exe` | str(512) | nullable |
| `windows` | JSON | list of window dicts, default `[]` |
| `screenshot_path` | str(512) | nullable; path only |
| `screenshot_phash` | str(32) | nullable, indexed |
| `screenshot_skipped_reason` | str(64) | nullable, see below |
| `messaging_present` | bool | a non-minimized messaging window was visible |
| `screenshot_redacted` | bool | the saved PNG had messaging windows blacked out |
| `game_running` | bool | installed game process detected (Steam library plus curated list) |
| `same_as_prev` | bool | the screenshot is a perceptual-hash duplicate of a recent one and was not saved |

`screenshot_skipped_reason` values: `session_inactive`, `session_locked` (logind says the graphical session is not active or is locked), `messaging_app` (redaction could not be done safely, so no screenshot was kept), or NULL. Window metadata is still recorded on skipped ticks. Session detection is fail-open: if logind cannot be queried, capture continues.

Privacy invariant: a capture with `messaging_present` true must not have a saved screenshot unless `screenshot_redacted` is true. `familymonctl health` counts violations over all time.

### 4.2 Table `classification`

| Column | Type | Notes |
|---|---|---|
| `id` | int | primary key |
| `capture_id` | FK `capture.id` | `ON DELETE CASCADE`, unique (one classification per capture) |
| `category` | str(32) | indexed; the single winning category |
| `confidence` | float | |
| `evidence_text` | str(1024) | nullable |
| `source` | str(32) | indexed |
| `schema_version` | int | default 1 |
| `present_categories` | JSON | distinct categories among visible windows in priority order; `[0]` is the winner |
| `raw_response` | JSON | nullable |
| `created_at` | datetime (tz) | |

Composite index `ix_classification_category_created` on (`category`, `created_at`).

Categories: `school`, `homework`, `games`, `social`, `messaging`, `video`, `code`, `productivity`, `browsing_other`, `idle`, `unknown`. Priority when several windows are visible (lowest wins): games, video, social, school, homework, code, productivity, messaging, browsing_other, idle, unknown. Nothing in the defaults assigns `homework`. An error while classifying one row writes `unknown` with confidence 0.0 and evidence `classifier error: ...`.

`source` values: `phash`, `heuristic`, `domain`, `game_process`, `ocr_kw`, `local_vlm`, `claude_code_bootstrap`. Only `heuristic`, `domain` and `game_process` are produced today; the others are reserved. `classify --redo` deletes only rows whose source is `heuristic`, `domain` or `game_process` and recomputes them.

Parent decides: the priority rule is policy. Do not reorder it on the strength of one ambiguous example; see [../concepts.md](../concepts.md).

### 4.3 Table `event`

| Column | Type | Notes |
|---|---|---|
| `id` | int | primary key |
| `at` | datetime (tz) | indexed |
| `kind` | str(32) | indexed |
| `data` | JSON | nullable |

### 4.4 Migration

At open, missing columns are added with `ALTER TABLE` (idempotent): `capture.messaging_present`, `capture.screenshot_redacted`, `capture.game_running`, `classification.present_categories`. No other migration mechanism exists.

### 4.5 Permissions, integrity and self-recovery

Every open goes through `make_engine`, which:

1. Creates the DB parent directory 0700 and chmods the DB, `-wal`, `-shm` to 0600.
2. Runs an integrity check first: `PRAGMA quick_check(5)` on a read-only connection. A "locked" or "busy" error counts as healthy.
3. On damage, moves the file aside as `captures.db.corrupt-<YYYYmmdd-HHMMSS>` (siblings too) and recovers into a fresh DB, trying in order: `sqlite3 <damaged> .recover` piped to a new DB (needs the `sqlite3` binary), Python `iterdump`, then an empty DB. The damaged file is never deleted.
4. Records a `db_recovered` event.

Consequence: any command that opens the DB, including `health`, `conflicts` and the dashboard, can create files, change modes or trigger recovery. They are read-only in intent, not in effect.

Inspect safely (read-only, as the DB owner or root):

```
sqlite3 'file:/home/kid/.local/state/familymon/captures.db?mode=ro' 'select kind, count(*) from event group by kind;'
```

Safety: the DB contains window titles. Query aggregates where possible; do not paste rows into public places.

## 5. Event kinds

The only code paths that insert `event` rows are the ones below (enforce commands share an `_audit` helper in `familymon/cli.py`). Only `db_recovered` is read back by code (`health`). The `Event` model docstring mentions "capture errors", but no emitter exists.

| kind | Written by | Which DB | `data` keys | Meaning |
|---|---|---|---|---|
| `heartbeat` | `heartbeat-once` (user timer) | the kid's own DB | `status`, `age_seconds`, `last_capture_at` | Freshness of the newest capture. `status` is `ok` (under 180 s), `stale` (180 to under 600 s), `dead` (600 s or more), `never` (no captures). A gap in heartbeat events means the watcher itself was down. |
| `db_recovered` | DB open (`make_engine`) | the DB being opened | `method`, `finding`, `damaged_backup`, `salvaged_tables` | A damaged DB was set aside and rebuilt. `method` is `sqlite3-recover`, `iterdump` or `fresh`. `health` prints it. |
| `playtime_chargeback` | `enforce chargeback` | root DB | `user`, `captures`, `seconds`, `last_capture_id`, `capped` | Browser-game time was charged against PlayTime. `capped` is true when the per-run cap (1800 s) applied. |
| `grant_hours` | `enforce grant` (`--until`) | root DB | `user`, `day`, `start`, `end`, `note` | A date-bound extension of allowed hours was granted. |
| `grant_playtime` | `enforce grant` (`--playtime`) | root DB | `user`, `seconds`, `note` | Extra PlayTime was granted. |
| `web_block` | `enforce web-block` | root DB | `user`, `active`, `subject`, `domains` | The Chrome policy file was changed on disk by this run (for example a lesson started or ended, or the config changed). `active` is true while a lesson blocks; `domains` is the blocklist size (0 when inactive). |
| `lesson_close` | `enforce web-block` sweep | root DB | `user`, `subject`, `apps`, `pids`, `forced` | During a lesson, forbidden processes were closed (SIGTERM, then SIGKILL after a 3 s grace). `apps` are rule names, `pids` all closed pids, `forced` the subset of pids that survived SIGTERM and got SIGKILL. Forbidden means other browsers, Chrome started with `--disable-extensions`, or `web_block.forbid_apps` matches. |

`heartbeat-once` exit codes: `ok` 0, `stale` 1, `dead` 2, `never` 3. `health` exit codes: 2 on a privacy violation, 4 if DB integrity is not ok (the privacy check runs first).

## 6. Other state sources worth knowing

- **timekpr-next** holds the real time and app enforcement state (the global PlayTime switches in `/etc/timekpr/timekpr.conf`, and per-user settings). familymon reads it over the system D-Bus (`enforce show`) and writes it through `timekpra`. See [../operate/troubleshoot.md](../operate/troubleshoot.md) for gotchas. Per `timekpr_admin.py`, `timekpra --getuserinfo` prints every enabled boolean as False, which is why familymon reads state over D-Bus. familymon never reads or writes `/etc/timekpr/timekpr.conf`; the PlayTime switch names are in [../operate/troubleshoot.md](../operate/troubleshoot.md).
- **KWin and logind** are the live sources for window lists and session state. familymon registers the session-bus name `org.familymon.WindowSink` (path `/sink`) to receive window lists from a temporary KWin script. Any same-uid process on the session bus could forge that call (known limitation, noted in code).
- **Chrome policy** is a derived artifact: delete or regenerate it with `enforce web-block`, never hand-edit it (the minute timer overwrites it). The document always carries a base policy (`DnsOverHttpsMode`, `EncryptedClientHelloEnabled`, `DeveloperToolsAvailability`); `URLBlocklist` and `URLAllowlist` are present only while a lesson blocks. When the extension is packaged, the force-install entry and its `3rdparty` settings are present whenever `web_block` is enabled (with `active` false outside lessons). Incognito and Guest are disabled per `web_block.block_private`.
- **Extension files** under `/usr/share/familymon/chrome-extension/` must carry a higher `version` on each rebuild or Chrome keeps the old one. Chrome fetches `update.xml` and the `.crx` through `file://` URLs.

## 7. Where-is-it quick index

| Question | Look at |
|---|---|
| What was the kid doing at time T? | `capture` + `classification` in `~kid/.local/state/familymon/captures.db` |
| Is capture alive? | newest `heartbeat` event; `familymonctl health`; `systemctl --user status familymon-capture.service` |
| Why is a row `unknown`? | `classification.evidence_text` and `source`; unclassified rows have no classification row yet (classify timer lag) |
| Was a screenshot redacted or skipped? | `capture.screenshot_redacted`, `screenshot_skipped_reason`, `messaging_present` |
| Current lessons and breaks | `~kid/.local/state/familymon/schedule.yaml`; extras in `/etc/familymon/schedule_extras.yaml` |
| Enforcement policy | `/var/lib/familymon/policy.yaml` |
| One-off extensions | `/var/lib/familymon/grants.json`; audit in root DB `grant_hours` / `grant_playtime` events |
| PlayTime charged today | `/var/lib/familymon/playtime_charges.json` |
| Did a notification fire today? | `notified.json`, `warned.json` (and the desktop's notification history; never a raw screenshot) |
| Web-block state | `/etc/opt/chrome/policies/managed/familymon.json`; `web_block` and `lesson_close` events |
| DB was repaired? | `captures.db.corrupt-*` beside the DB; `db_recovered` event |

## 8. Unverified or unsettled points

- Whether the package or any script creates `/var/lib/familymon` and a `familymon` system user: none found.
- Whether the dashboard is meant for the kid's or the parent's account: unit is user-scope, reads the running user's DB.
- `GET /api/captures` and `/api/stats` with an invalid `day` likely return a server error (unhandled `ValueError`); inferred, not verified. `/d/{day}` returns 400.
- Exact timestamp timezone handling differs between tables (declared tz-aware; some code writes and compares local-naive values).
