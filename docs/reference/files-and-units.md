# Files, units, database and events

Read this when you need to find where a piece of familymon state lives (a file, a directory, a systemd unit, a database column or an event row), what permissions it has, what writes it, or how to inspect it safely. It is a lookup reference. For command syntax see [cli.md](cli.md); for policy and config keys see [config.md](config.md); for concepts see [../concepts.md](../concepts.md). Install steps: [../operate/install.md](../operate/install.md). Health checks: [../operate/verify-and-health.md](../operate/verify-and-health.md). Failures: [../operate/troubleshoot.md](../operate/troubleshoot.md).

Placeholders used throughout: the monitored account is `kid`, the laptop is `kid-laptop`, the parent is "the parent".

**Safety.** The capture database and screenshots hold the kid's activity. Read them only as the parent's agent, never copy them into public places, and never take raw screenshots of the kid's session for diagnostics (that bypasses messaging redaction). The chargeback job, `familymonctl events`, `familymonctl personal-time`, the MCP `events`, `timeline` and `personal_time` tools and the dashboard open the database read-only (SQLite URI `mode=ro`). Everything under `/var/lib/familymon` is root-owned and unreadable by the kid.

## 1. State directory resolution

`state_dir()` (`familymon/config.py`) resolves in this order:

1. `$FAMILYMON_STATE_DIR`, if set (the dashboard unit sets it to `/var/lib/familymon`).
2. `/var/lib/familymon`, if the effective uid is 0 (production).
3. `${XDG_STATE_HOME:-~/.local/state}/familymon` (dev runs only).

`monitored_user()` is `$FAMILYMON_USER`, else the `user:` key of `policy.yaml`.

`user_state_dir(user)` is `<home of user>/.local/state/familymon`, where releases before the root deployment kept the kid's data. Only `familymonctl migrate-state` reads it now. Nothing in production writes there.

A command run as your own login user (dev or inspection) sees your own `~/.local/state/familymon`, not `/var/lib/familymon`. Run production commands with `sudo`. `familymonctl status` prints the resolved state dir, DB path and screenshots dir (read-only).

## 2. Files and directories

### 2.1 State: `/var/lib/familymon/`

Owner root, group `familymon`, mode 0750, created by the postinst. Written only by root jobs (capture supervisor, classify, heartbeat, notify, enforce, chargeback, web-block) and root operator commands (`policy set`, `schedule import`, ..., directly or through `familymonctl mcp`). The monitored user cannot open it. `storage/perms.py` applies the modes below whenever root creates or opens the files.

Group-readable by `familymon` (the read-only dashboard's group): dirs 0750, files 0640, owner root.

| Path | Written by | Contents |
|---|---|---|
| `captures.db` (plus `-journal`, `-wal`, `-shm`) | `capture-supervise`, `classify`, `heartbeat-once`, enforcement commands, `policy set`, `config set` (events only) | SQLite DB, see section 4. The only database; holds captures, classifications and all events (section 5) |
| `captures.db.corrupt-<YYYYmmdd-HHMMSS>` | DB self-recovery | the damaged original, kept, never deleted; `-journal`, `-wal`, `-shm` siblings are renamed with it |
| `screenshots/<YYYY-MM-DD>/<capture_id>.png` | capture supervisor | one PNG per persisted capture; the DB stores only the path. Root-only: day directories 0700, files 0600. Day directories dated older than `retention.screenshots_days` (default 90) are deleted by `familymon-retention.timer` |
| `schedule.yaml` | `schedule import`, `schedule refresh`, or by hand | keys `timezone`, `blocks`, optional `breaks`. Read by every consumer. `schedule import` and `schedule refresh` leave it root:`familymon` 0640 so the dashboard can read it; a hand-written file needs the same mode |

Root only (0600, owner root):

| Path | Written by | Contents |
|---|---|---|
| `policy.yaml` | `policy set` (CLI or the `policy_set` MCP tool), or the parent by hand | enforcement policy and monitored `user:`, see [config.md](config.md). `policy set` writes it verbatim, comments kept, through a temp file and a rename. If missing, `enforce` fails and points to `policy set` and `/usr/share/familymon/policy_example.yaml`, and the capture, notify, enforce, chargeback, webblock and retention units skip themselves (`ConditionPathExists`) |
| `policy.yaml.prev` | `policy set` | the policy as it was before the last `policy set`; one level of undo (`sudo familymonctl policy set /var/lib/familymon/policy.yaml.prev`). Absent until the first `policy set` that replaced an existing file |
| `schedule.yaml.prev` | `schedule import`, `schedule refresh` | the schedule as it was before the last import or refresh; one level of undo (copy it back over `schedule.yaml` as root, keeping root:`familymon` 0640) |
| `known_games.json.prev` | `enforce games --forget` | the remembered-games list before the last `--forget`; copy it back by hand as root |
| `grants.json` | `enforce grant`, `enforce revoke` | `{"grants":[{user, day, start, end, created_at, note}]}`. Expired or other-day grants are pruned when active grants are evaluated (`GrantStore.active`) and when one is added; `enforce revoke` rewrites the file without the user's grants for today |
| `playtime_charges.json` | `enforce chargeback` | `{user: {day, last_capture_id, charged_seconds_today}}`. `charged_seconds_today` resets on a new day; the `last_capture_id` watermark is kept across days, but is reset for today if it is above the DB's newest capture id (DB replaced, recovered or migrated) |
| `notified.json` | `schedule notify-tick` | `{"<ISO date>": ["HH:MM\|subject", ...]}`; overwritten with only today's key; makes block-start notifications fire at most once per day. An entry is written only when the notification was delivered |
| `warned.json` | `schedule notify-tick` | `{"<key>": "<ISO timestamp>"}`; entries from other days pruned; drives the live-warning cooldown. Written only when the warning was delivered |
| `personal_time_notified.json` | `schedule notify-tick` | `{"day": "<ISO date>", "until": "<ISO timestamp>"}`; the day the personal-time notice was delivered, so it fires once a day. Written only when delivered |
| `known_games.json` | capture supervisor, `enforce games --forget` | game directories seen installed, so hiding a game's files does not remove its PlayTime mask. List with `enforce games` |

Migration leftovers (root-only): `migrated/<user>-<YYYYmmdd-HHMMSS>/` (the old per-user state directory, kept as a backup by `migrate-state`) and the marker `.migrated-from-<user>`.

Runtime (not in the state dir): `/run/familymon/kwin/` (root, 0711, created by the unit's `RuntimeDirectory=familymon`) holds the current tick's KWin window-list script, `winlist-<random>.js`, mode 0644, deleted after the tick.

A screenshot is saved only when image bytes exist and the capture is not `same_as_prev`. Screenshots are skipped for suspended sessions, for messaging windows that could not be redacted, and when the KWin window list did not arrive (see section 4.1).

**Retention.** Screenshot files older than `retention.screenshots_days` (default 90) are deleted once a day by `familymon-retention.timer`, which also clears the rows' `screenshot_path`. No code prunes `capture`, `classification` or `event` rows; they are about a kilobyte each. `familymonctl health` reports PNG count and size. If you delete screenshots by hand, leave the DB rows (the DB stores paths only, and the dashboard reports `screenshot_on_disk` as a boolean).

`enforce chargeback` and `familymonctl events` open the DB strictly read-only (`mode=ro`); the dashboard does the same through its group access. `enforce web-block` reads `schedule.yaml` from this directory (unless `--schedule FILE` is given).

### 2.3 Configuration lookup

`config_file(name)` resolves in this order:

1. `$FAMILYMON_CONFIG_DIR/<name>`
2. `/etc/familymon/<name>`
3. The bundled default in the package (`familymon/defaults/`)

Bundled defaults: `messaging_apps.yaml`, `game_apps.yaml`, `schedule_extras.yaml`, `domains_starter.yaml`, `title_brands.yaml`, `policy_example.yaml`. Only the first three are installed to `/etc/familymon/` by the deb; the others are used from the bundle unless you place an override in `/etc/familymon/` or `$FAMILYMON_CONFIG_DIR`.

`familymonctl config set <name>` (or the `config_set` MCP tool) writes the override for the first five names: to `$FAMILYMON_CONFIG_DIR/<name>` when that variable is set, else `/etc/familymon/<name>`, mode 0644 (the capture probe runs as the kid and reads `messaging_apps.yaml`). Before writing it copies the previous override to `<name>.prev` in the same directory (one level of undo) and, during validation only, creates and deletes a temporary `.<name>.validate` there. `config list` shows which file is in effect for each name; `config get <name>` prints it.

### 2.4 Package payload (deb)

| Path | Contents | Mode |
|---|---|---|
| `/opt/familymon/venv` | bundled Python venv, built from `uv.lock` with the runtime extras `dashboard`, `analyze` and `mcp` (the MCP SDK, `mcp>=2.3`) | n/a |
| `/usr/bin/familymonctl` | wrapper that runs `/opt/familymon/venv/bin/python3 -m familymon.cli` | 0755 |
| `/etc/familymon/messaging_apps.yaml` | conffile: window classes and title patterns that trigger redaction | 0644 |
| `/etc/familymon/game_apps.yaml` | conffile: curated game window classes and binaries | 0644 |
| `/etc/familymon/schedule_extras.yaml` | conffile: schedule exclusions, custom blocks, breaks | 0644 |
| `/etc/sudoers.d/familymon-mcp` | conffile: `%familymon-admin ALL=(root) NOPASSWD: /usr/bin/familymonctl mcp`, so the parent's agent can run `ssh kid-laptop sudo -n familymonctl mcp` (see [mcp.md](mcp.md)). Checked with `visudo -c` when the deb is built | 0440 |
| `/usr/share/familymon/policy_example.yaml` | example policy | 0644 |
| `/usr/share/familymon/chrome-extension/{familymon.crx,update.xml,id,version}` | built lesson-block extension | 0644 |
| `/usr/share/applications/familymon.desktop` | `NoDisplay=true`; its name is passed as the `desktop-entry` hint so Plasma shows familymon notifications as popups | 0644 |
| `/usr/lib/systemd/system/familymon-*` | system units (section 3). The package ships no user units | 0644 |

Not shipped: `/etc/familymon/dashboard.env` (you create it; section 3.4) and `policy.yaml` (you copy it from the example).

**postinst** (runs only on `configure`, always exits 0):

1. Creates the `familymon` system user and group (`adduser --system --group --no-create-home`) if missing, and the system group `familymon-admin` (`addgroup --system`) if missing. `familymon-admin` is left empty: the parent adds their own account (`sudo usermod -aG familymon-admin parent`, effective at their next login). The kid's account must never be a member.
2. Forces `/etc/familymon/dashboard.env`, if present, to root:root 0600.
3. Makes `/var/lib/familymon` root:`familymon` 0750.
4. Retires per-user units of earlier releases: stops them in every running user manager, kills old `familymon.cli capture-run|dashboard` processes, and deletes `familymon-*` symlinks (enable and mask links) under every home's `~/.config/systemd/user` with `find -delete`, which does not follow symlinks.
5. Runs `familymonctl migrate-state` if `/var/lib/familymon/policy.yaml` exists (prints a re-run hint on failure).
6. `systemctl daemon-reload`, enables `familymon-capture.service`, `familymon-dashboard.service` and the seven timers (classify, heartbeat, notify, enforce, chargeback, webblock, retention), restarts the two services and starts the timers.

The dpkg conffiles are `/etc/familymon/messaging_apps.yaml`, `/etc/familymon/game_apps.yaml`, `/etc/familymon/schedule_extras.yaml` and `/etc/sudoers.d/familymon-mcp`; dpkg keeps local edits to them on upgrade.

**prerm** (on `remove` only): `systemctl disable --now` for the two services and seven timers. There is no postrm or preinst. `Depends` includes `adduser`, `util-linux` (for `runuser`), `procps` and `findutils`.

### 2.5 Other paths

| Path | Purpose |
|---|---|
| `/etc/opt/chrome/policies/managed/familymon.json` | Chrome managed policy, written by `enforce web-block` as root (mode 0644). Written atomically via `/etc/opt/chrome/policies/.familymon.json.tmp` then rename. Deleted when `web_block` is disabled in `policy.yaml`. |
| `/etc/familymon/<name>.prev` | the previous override, kept by `config set <name>` (one level of undo), mode as the override (0644). In `$FAMILYMON_CONFIG_DIR` instead when that is set |
| `/etc/sudoers.d/familymon-mcp` | shipped by the deb (0440, conffile): lets members of `familymon-admin` start exactly `/usr/bin/familymonctl mcp` as root without a password. Nothing else is granted |
| group `familymon-admin` | system group created empty by the postinst; its members (the parent only) may start the MCP server through the rule above. Check with `getent group familymon-admin` |
| `/etc/familymon/dashboard.env` | env file with `FAMILYMON_DASH_USER` and `FAMILYMON_DASH_PASS` (and optionally bind and port); root:root 0600, read by systemd as root. The dashboard unit is skipped if it is missing |
| `/run/familymon/kwin/winlist-<random>.js` (temp) | the supervisor's per-tick KWin enumeration script, deleted after the tick (dev `capture-run` uses `familymon_winlist_*.js` in `/tmp`) |
| `familymon_shot_*.png` (temp) | per-tick spectacle output, in the capture unit's private `/tmp`, deleted after reading |
| `${XDG_STATE_HOME:-~/.local/state}/familymon-deps-up.log` | dev-machine nightly dependency-upgrade log, trimmed to last 1000 lines |
| `<repo>/.git/uv-upgrade.lock` | dev-machine lock for `scripts/uv-upgrade.sh` |
| `dist/familymon_<VERSION>_amd64.deb`, `dist/repo` | deb build output and local apt repo staging (dev machine) |
| `private/chrome-extension-key.pem`, `private/deploy.conf`, `private/publish-denylist.txt` | extension signing key (0600), deploy settings, pre-push denylist. `private/` is gitignored. |

Safety: the extension signing key is generated per deployment (`openssl genrsa 2048`). A lost key only changes the extension id. Do not publish `private/` contents.

### 2.6 Environment variables

| Variable | Effect |
|---|---|
| `FAMILYMON_STATE_DIR` | overrides the state directory (set by the dashboard unit) |
| `FAMILYMON_USER` | the monitored account; default is `user:` in `policy.yaml` |
| `FAMILYMON_CONFIG_DIR` | first location searched for config YAML |
| `FAMILYMON_INTERVAL` | capture interval in seconds, default 60 |
| `FAMILYMON_DASH_USER`, `FAMILYMON_DASH_PASS` | required dashboard credentials (HTTP Basic, realm `familymon`); the app refuses to start if either is unset or empty |
| `FAMILYMON_DASH_BIND`, `FAMILYMON_DASH_PORT` | dashboard bind (default `127.0.0.1`) and port (default `8765`); `--bind` and `--port` override |
| `FAMILYMON_APT_HOST` | dev-machine publish target for `scripts/publish-deb.sh` |
| `FAMILYMON_DENYLIST` | pre-push hook denylist path |

## 3. systemd units

All production units are system units, installed to `/usr/lib/systemd/system/` and enabled by the postinst. The deb ships no user units. Dev-only user units under `deploy/systemd/user/` (hard-coded to a developer checkout) exist for running `familymonctl capture-run` inside your own session; they are not packaged, so do not use them for installs.

### 3.1 Unit table

All run as root except the dashboard.

| Unit | Schedule / trigger | Runs |
|---|---|---|
| `familymon-capture.service` | `WantedBy=multi-user.target`, `After=systemd-logind.service`, `ConditionPathExists=/var/lib/familymon/policy.yaml`; `Restart=always`, `RestartSec=10`, `Nice=10`, `PrivateTmp=yes`, `RuntimeDirectory=familymon` (mode 0755) | `/usr/bin/familymonctl capture-supervise` |
| `familymon-classify.timer` | `OnActiveSec=2min`, `OnUnitActiveSec=5min`, `Persistent=true` | triggers `familymon-classify.service` |
| `familymon-classify.service` | oneshot, `Nice=15` | `familymonctl classify` |
| `familymon-heartbeat.timer` | `OnActiveSec=1min`, `OnUnitActiveSec=1min`, `Persistent=true` | triggers `familymon-heartbeat.service` |
| `familymon-heartbeat.service` | oneshot; non-zero exit marks the unit failed | `familymonctl heartbeat-once` |
| `familymon-notify.timer` | `OnCalendar=*-*-* *:*:05` (every minute, 5 s past), `AccuracySec=5s`, `Persistent=false` | triggers `familymon-notify.service` |
| `familymon-notify.service` | oneshot, `ConditionPathExists` policy; delivers into the kid's session through `runuser` | `familymonctl schedule notify-tick` |
| `familymon-dashboard.service` | `User=familymon`, `Group=familymon`, `EnvironmentFile=/etc/familymon/dashboard.env`, `Environment=FAMILYMON_STATE_DIR=/var/lib/familymon`, `ConditionPathExists=/etc/familymon/dashboard.env`, `Restart=on-failure`, `RestartSec=10`, sandboxed (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectKernelTunables`, `ProtectKernelModules`, `ProtectControlGroups`, `RestrictSUIDSGID`), `WantedBy=multi-user.target` | `familymonctl dashboard` |
| `familymon-enforce.timer` | `OnActiveSec=5min`, `OnUnitActiveSec=1h`, `RandomizedDelaySec=5min`, `Persistent=true` | triggers `familymon-enforce.service` |
| `familymon-enforce.service` | oneshot, `After=timekpr.service`, `ConditionPathExists` policy; exit 3 means the apply guard refused (unit shows failed) | `familymonctl enforce apply` |
| `familymon-chargeback.timer` | `OnActiveSec=3min`, `OnUnitActiveSec=5min`, `Persistent=true` | triggers `familymon-chargeback.service` |
| `familymon-chargeback.service` | oneshot, `After=timekpr.service`, `ConditionPathExists` policy | `familymonctl enforce chargeback` |
| `familymon-webblock.timer` | `OnCalendar=*-*-* *:*:00` (every minute), `AccuracySec=5s`, `Persistent=false` | triggers `familymon-webblock.service` |
| `familymon-webblock.service` | oneshot, `ConditionPathExists` policy | `familymonctl enforce web-block` |
| `familymon-retention.timer` | `OnCalendar=*-*-* 03:00:00`, `RandomizedDelaySec=1h`, `Persistent=true` | triggers `familymon-retention.service` |
| `familymon-retention.service` | oneshot, `ConditionPathExists` policy, `Nice=15`, `IOSchedulingClass=idle` | `familymonctl retention` |

A service skipped by `ConditionPathExists` shows as inactive, not failed. A skipped service is not retried: after creating `policy.yaml`, run `sudo systemctl restart familymon-capture.service` (the timer-driven ones pick it up on their next tick). The capture unit is the only long-running supervisor; the kid cannot stop or mask any of these.

`PrivateTmp=yes` on the capture unit means the probe's temporary screenshot file (written by `spectacle` as the kid) sits in a `/tmp` the kid cannot see.

### 3.2 Inspect and undo

```
systemctl status familymon-capture.service
systemctl list-timers 'familymon-*'
journalctl -u familymon-capture.service --since '-30min'
```

Undo: `sudo systemctl disable --now <unit>` (the next package upgrade re-enables it). Safety: disabling the capture, heartbeat or enforce units stops monitoring or enforcement for the kid. Parent decides; tell the parent first. `familymon-enforce.timer` applies the policy hourly and will revert manual changes to allowed hours (use `enforce grant`; see [cli.md](cli.md)). Disabling `familymon-webblock.timer` leaves the last-written Chrome policy file in place until `enforce web-block` runs again.

### 3.3 Stale code after upgrade

Long-running units (capture supervisor, dashboard) can keep running old code after a package upgrade. The postinst restarts them. If behaviour looks stale, compare the unit's `ActiveEnterTimestamp` with the package install time:

```
systemctl show familymon-capture.service -p ActiveEnterTimestamp
dpkg-query -W -f='${Version}\n' familymon
```

### 3.4 Dashboard notes

`familymon-dashboard.service` runs as the unprivileged `familymon` user with `FAMILYMON_STATE_DIR=/var/lib/familymon`. It opens the DB read-only and reads the schedule through group access (it never reads screenshot files); it cannot read `policy.yaml` or anything else root-only. It needs `FAMILYMON_DASH_USER` and `FAMILYMON_DASH_PASS` from `/etc/familymon/dashboard.env` (root:root 0600, read by systemd before privileges drop). Auth is HTTP Basic with no TLS; the only exposure control is the bind address (default `127.0.0.1`). Routes: `GET /`, `GET /d/{YYYY-MM-DD}`, `GET /api/captures?day=`, `GET /api/stats?day=`. It serves metadata only, never image bytes.

The status banner reads the newest `heartbeat` event (falling back to plain capture freshness when there is none): OK, stale, "Capture DOWN" (with "while the session is <state>" when known), "Not logged in" (`idle`), "Heartbeat watcher not running" (newest heartbeat older than 5 minutes), or no captures yet. A red line under it appears when the day has `capture_failed` or `capture_tamper` events: "N capture(s) failed while the session was open; M forged window-list attempt(s) rejected", pointing at `sudo familymonctl events --day YYYY-MM-DD`. Captures flagged `personal_time` keep their category pill, show a grey "personal time" tag instead of the app, and carry no title; a note above the timeline gives the span ("Personal time 20:00–21:59: no screenshots; app names and titles hidden").

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
| `personal_time` | bool | captured inside the policy's `personal_time` window: no screenshot was taken, and the dashboard and API hide the app, title, evidence and skip reason (the category is kept) |
| `same_as_prev` | bool | the screenshot is a perceptual-hash duplicate of a recent one and was not saved |

`screenshot_skipped_reason` values: `session_inactive`, `session_locked` (logind says the graphical session is not active or is locked), `messaging_app` (redaction could not be done safely, so no screenshot was kept), `window_list_unavailable` (the KWin window list did not arrive, so no screenshot was taken; redaction needs the geometry), or NULL. Window metadata is still recorded on skipped ticks. Session detection is fail-open: if logind cannot be queried, capture continues.

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

At open, missing columns are added with `ALTER TABLE` (idempotent): `capture.messaging_present`, `capture.screenshot_redacted`, `capture.game_running`, `capture.personal_time`, `classification.present_categories`. No other migration mechanism exists.

### 4.5 Permissions, integrity and self-recovery

Every open goes through `make_engine`, which:

1. Creates the DB parent directory and secures the DB, `-journal`, `-wal`, `-shm` and the directory (`storage/perms.py`): as root in production they become root-owned and group-readable by `familymon` (files 0640, directory 0750); in a dev run they are 0600 and 0700.
2. Runs an integrity check first: `PRAGMA quick_check(5)` on a read-only connection. A "locked" or "busy" error counts as healthy.
3. On damage, moves the file aside as `captures.db.corrupt-<YYYYmmdd-HHMMSS>` (siblings too) and recovers into a fresh DB, trying in order: `sqlite3 <damaged> .recover` piped to a new DB (needs the `sqlite3` binary), Python `iterdump`, then an empty DB. The damaged file is never deleted.
4. Records a `db_recovered` event.

Consequence: any command that opens the DB read-write, including `health` and `conflicts`, can create files, change modes or trigger recovery. They are read-only in intent, not in effect. The dashboard, `events` and chargeback use `make_readonly_engine`, which does none of that.

Inspect safely (read-only, as root):

```
sudo sqlite3 'file:/var/lib/familymon/captures.db?mode=ro' 'select kind, count(*) from event group by kind;'
```

Safety: the DB contains window titles. Query aggregates where possible; do not paste rows into public places.

## 5. Event kinds

The only code paths that insert `event` rows are the ones below. The operator functions in `familymon/ops.py` (grants, `policy set`, `config set`, `enforce apply`, shared by the CLI and the MCP tools) write through its `_audit` helper, which appends only when the DB already exists; `enforce web-block` uses the `_audit` helper in `familymon/cli.py`. They all land in the one DB. Read them with `sudo familymonctl events [--day D] [--kind K]...` (heartbeats are left out unless `--kind heartbeat` is given). `db_recovered` is read back by `health`; `heartbeat` by the dashboard banner; `capture_failed` and `capture_tamper` by the dashboard's red line.

| kind | Written by | Which DB | `data` keys | Meaning |
|---|---|---|---|---|
| `heartbeat` | `heartbeat-once` (root timer, every minute) | the DB | `status`, `session`, `age_seconds`, `last_capture_at` | Freshness of the newest capture and the kid's session. `status` is `ok` (under 180 s), `stale` (180 to under 600 s), `dead` (600 s or more), `never` (no captures), `idle` (no graphical session, nothing fresh). `session` is `active`, `locked`, `inactive`, `none` or `unknown` (logind). A gap in heartbeat events means the watcher timer was down; the dashboard shows that as "Heartbeat watcher not running" after 5 minutes. |
| `capture_failed` | capture supervisor | the DB | `error`, `session` | A tick ran while the kid had a session, but the probe failed (timeout, crash, could not start) or the KWin window list did not arrive. No capture was written for that tick. See [../operate/troubleshoot.md](../operate/troubleshoot.md). |
| `capture_tamper` | capture supervisor | the DB | `sink_rejected`, `session` | The window-list sink rejected a push: wrong D-Bus sender or wrong nonce. Something other than KWin's own script tried to deliver a window list. |
| `db_recovered` | DB open (`make_engine`) | the DB being opened | `method`, `finding`, `damaged_backup`, `salvaged_tables` | A damaged DB was set aside and rebuilt. `method` is `sqlite3-recover`, `iterdump` or `fresh`. `health` prints it. |
| `playtime_chargeback` | `enforce chargeback` | the DB | `user`, `captures`, `seconds`, `last_capture_id`, `capped` | Browser-game time was charged against PlayTime. `capped` is true when the per-run cap (1800 s) applied. |
| `screenshots_pruned` | `retention` | the DB | `keep_days`, `cutoff`, `files`, `bytes`, `dirs`, `rows`, `first_day`, `last_day` | Screenshot files older than the retention age were deleted and the affected rows' `screenshot_path` cleared. Written only when something was deleted. |
| `grant_hours` | `enforce grant` (`--until`), MCP `grant` (`until`) | the DB | `user`, `day`, `start`, `end`, `note` | A date-bound extension of allowed hours was granted. |
| `grant_playtime` | `enforce grant` (`--playtime`), MCP `grant` (`playtime`) | the DB | `user`, `seconds`, `note` | Extra PlayTime was granted. |
| `policy_changed` | `policy set`, MCP `policy_set` | the DB, if it exists | `changed`, `previous` | `policy.yaml` was replaced. `changed` lists the top-level keys that differ from the old file; `previous` is the path of the kept copy (`policy.yaml.prev`), or null when there was no policy before. Nothing was applied. |
| `config_changed` | `config set`, MCP `config_set` | the DB, if it exists | `name`, `previous` | A config override was written. `previous` is the path of `<name>.prev`, or null when there was no override before. |
| `policy_applied` | `enforce apply` (hourly `familymon-enforce.timer`, by hand, or MCP `enforce_apply`) | the DB, if it exists | `user`, `changes`, `forced` | The policy was applied to timekpr and something changed. `changes` lists the setting names; `forced` is true when `--force` (or `force=true`) was given. Not written when nothing changed or when apply refused. Grants and revokes do not write it. |
| `web_block` | `enforce web-block` | the DB | `user`, `active`, `subject`, `domains` | The Chrome policy file was changed on disk by this run (for example a lesson started or ended, or the config changed). `active` is true while a lesson blocks; `domains` is the blocklist size (0 when inactive). |
| `lesson_close` | `enforce web-block` sweep | the DB | `user`, `subject`, `apps`, `pids`, `forced` | During a lesson, forbidden processes were closed (SIGTERM, then SIGKILL after a 3 s grace). `apps` are rule names, `pids` all closed pids, `forced` the subset of pids that survived SIGTERM and got SIGKILL. Forbidden means other browsers, Chrome started with `--disable-extensions`, or `web_block.forbid_apps` matches. |

`heartbeat-once` exit codes: `ok` 0, `idle` 0, `stale` 1, `dead` 2, `never` 3. `health` exit codes: 2 on a privacy violation, 4 if DB integrity is not ok (the privacy check runs first).

## 6. Other state sources worth knowing

- **timekpr-next** holds the real time and app enforcement state (the global PlayTime switches in `/etc/timekpr/timekpr.conf`, and per-user settings). Familymon reads it over the system D-Bus (`enforce show`) and writes it through `timekpra`. See [../operate/troubleshoot.md](../operate/troubleshoot.md) for gotchas. Per `timekpr_admin.py`, `timekpra --getuserinfo` prints every enabled boolean as False, which is why familymon reads state over D-Bus. Familymon never reads or writes `/etc/timekpr/timekpr.conf`; the PlayTime switch names are in [../operate/troubleshoot.md](../operate/troubleshoot.md).
- **KWin and logind** are the live sources for window lists and session state. Each capture probe registers the session-bus name `org.familymon.WindowSink` (path `/sink`) to receive the window list from that tick's KWin script. The sink accepts a push only from KWin's own bus connection carrying the tick's nonce; anything else is rejected and recorded as `capture_tamper`. The script is unloaded after each tick. A same-uid process can still squat the bus name or kill the probe, which makes the tick fail (`capture_failed`); that is detected, not prevented. See [../develop/tamper-resistance.md](../develop/tamper-resistance.md).
- **Chrome policy** is a derived artifact: delete or regenerate it with `enforce web-block`, never hand-edit it (the minute timer overwrites it). The document always carries a base policy (`DnsOverHttpsMode`, `EncryptedClientHelloEnabled`, `DeveloperToolsAvailability`); `URLBlocklist` and `URLAllowlist` are present only while a lesson blocks. When the extension is packaged, the force-install entry and its `3rdparty` settings are present whenever `web_block` is enabled (with `active` false outside lessons). Incognito and Guest are disabled per `web_block.block_private`.
- **Extension files** under `/usr/share/familymon/chrome-extension/` must carry a higher `version` on each rebuild or Chrome keeps the old one. Chrome fetches `update.xml` and the `.crx` through `file://` URLs.

## 7. Where-is-it quick index

| Question | Look at |
|---|---|
| What was the kid doing at time T? | `capture` + `classification` in `/var/lib/familymon/captures.db` |
| Is capture alive? | newest `heartbeat` event; `sudo familymonctl health`; `systemctl status familymon-capture.service` |
| Did capture fail or get tampered with? | `sudo familymonctl events --kind capture_failed --kind capture_tamper` |
| Why is a row `unknown`? | `classification.evidence_text` and `source`; unclassified rows have no classification row yet (classify timer lag) |
| Was a screenshot redacted or skipped? | `capture.screenshot_redacted`, `screenshot_skipped_reason`, `messaging_present` |
| Current lessons and breaks | `/var/lib/familymon/schedule.yaml`; extras in `/etc/familymon/schedule_extras.yaml` |
| Enforcement policy | `/var/lib/familymon/policy.yaml` (`sudo familymonctl policy get`) |
| Who changed the policy, and when it went live | `policy_changed` and `policy_applied` events; the previous file is `/var/lib/familymon/policy.yaml.prev` |
| Which config override is in effect | `familymonctl config list`; `/etc/familymon/<name>` and `<name>.prev`; `config_changed` events |
| Who may start the MCP server | `/etc/sudoers.d/familymon-mcp`; `getent group familymon-admin` |
| One-off extensions | `/var/lib/familymon/grants.json`; audit in the `grant_hours` / `grant_playtime` events |
| PlayTime charged today | `/var/lib/familymon/playtime_charges.json` |
| Did a notification fire today? | `notified.json`, `warned.json` (and the desktop's notification history; never a raw screenshot) |
| Web-block state | `/etc/opt/chrome/policies/managed/familymon.json`; `web_block` and `lesson_close` events |
| DB was repaired? | `captures.db.corrupt-*` beside the DB; `db_recovered` event |

## 8. Unverified or unsettled points

- Exact timestamp timezone handling differs between tables (declared tz-aware; some code writes and compares local-naive values).
