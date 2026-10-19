# familymonctl CLI reference

Read this when you need the exact synopsis, options, root requirement, side effects or exit codes of a `familymonctl` command, or when you must decide whether a command is safe to run against a live system. For concepts see [concepts.md](../concepts.md). For paths, units and config keys see [config.md](config.md) and [files-and-units.md](files-and-units.md). For routine checks see [../operate/verify-and-health.md](../operate/verify-and-health.md); for failures see [../operate/troubleshoot.md](../operate/troubleshoot.md).

Side-effect classes used below:

| Class | Meaning |
|---|---|
| READ-ONLY | Reads files, DB or D-Bus state. Changes nothing (see caveats per command). |
| WRITES-STATE | Writes familymon files, DB rows or events. |
| TOUCHES-PROCESSES | Changes timekpr, Chrome policy, notifications, kills processes, or runs a long-lived process or server. |

"Root?" is what the code or help text requires. In production every command is run as root (`sudo familymonctl ...`), because all state is in `/var/lib/familymon` and the monitored user cannot read it. Few commands check the effective uid in code (`capture-supervise` and `migrate-state` do); where root is only needed in practice, this is noted. The old convention of running schedule and health commands as the kid (`sudo -u kid -H familymonctl ...`) no longer applies.

## Entry point

- Console script: `familymonctl = "familymon.cli:main"`. `python -m familymon.cli` is equivalent.
- The deb installs `/usr/bin/familymonctl`, a 4-line `/bin/sh` wrapper that runs `exec /opt/familymon/venv/bin/python3 -m familymon.cli "$@"`. It does no root check, sets no environment and passes arguments and exit code through. In a dev checkout use `uv run familymonctl ...`.
- Every command and group also accepts click's `--help`.

Global option (before the subcommand):

| Option | Type | Default | Meaning |
|---|---|---|---|
| `-v`, `--verbose` | flag | off | DEBUG-level logging. Default is INFO. |

```bash
familymonctl -v health
```

## Paths and environment that affect every command

State directory, first match wins:

1. `$FAMILYMON_STATE_DIR` (the dashboard unit sets it to `/var/lib/familymon`)
2. `/var/lib/familymon` when the effective uid is 0 (production)
3. `${XDG_STATE_HOME:-~/.local/state}/familymon` (dev runs only)

Under it: `captures.db`, `screenshots/`, `schedule.yaml`, `policy.yaml`. The `enforce` commands also use `grants.json`, `playtime_charges.json` and `known_games.json`; `schedule notify-tick` uses `notified.json` and `warned.json`.

The monitored account is `$FAMILYMON_USER`, else the `user:` key of `policy.yaml`. Only `migrate-state` still looks at the kid's home (`<home of kid>/.local/state/familymon`, where releases before the root deployment kept their data).

Config files (`schedule_extras.yaml`, `domains_starter.yaml`, ...) resolve in order: `$FAMILYMON_CONFIG_DIR/<name>`, `/etc/familymon/<name>`, bundled defaults.

| Variable | Default | Used by |
|---|---|---|
| `FAMILYMON_STATE_DIR` | see above | all |
| `FAMILYMON_CONFIG_DIR` | unset | config lookup |
| `FAMILYMON_USER` | `user:` in `policy.yaml` | `capture-supervise`, `heartbeat-once`, `migrate-state`, `schedule notify-tick` (the monitored account) |
| `FAMILYMON_INTERVAL` | `60` (seconds) | `capture-run`, `capture-supervise`, `conflicts` (duration estimate) |
| `FAMILYMON_DASH_USER`, `FAMILYMON_DASH_PASS` | none, both required | `dashboard` |
| `FAMILYMON_DASH_BIND` | `127.0.0.1` | `dashboard` |
| `FAMILYMON_DASH_PORT` | `8765` | `dashboard` |

Safety: a non-root run uses `~/.local/state/familymon`, which is a separate DB from `/var/lib/familymon`. Run production commands with `sudo`; a non-root run silently reports on a dev state dir.

## Index

| Command | Class | Root? | Safe preview |
|---|---|---|---|
| [`status`](#status) | READ-ONLY | no | itself |
| [`capture-once`](#capture-once) | WRITES-STATE, TOUCHES-PROCESSES | not enforced | none |
| [`capture-run`](#capture-run) | WRITES-STATE, TOUCHES-PROCESSES | not enforced (dev loop) | none |
| [`capture-supervise`](#capture-supervise) | WRITES-STATE, TOUCHES-PROCESSES | yes (refuses otherwise) | none |
| `capture-probe` (hidden, internal) | TOUCHES-PROCESSES | no | none |
| [`migrate-state`](#migrate-state) | WRITES-STATE | yes (refuses otherwise) | none (idempotent) |
| [`events`](#events) | READ-ONLY | yes in practice | itself |
| [`retention`](#retention) | WRITES-STATE (deletes files) | yes in practice | `--dry-run` |
| [`personal-time`](#personal-time) | READ-ONLY | yes in practice | itself |
| [`classify`](#classify) | WRITES-STATE | not enforced | none |
| [`health`](#health) | READ-ONLY (see caveat) | no | itself |
| [`heartbeat-once`](#heartbeat-once) | WRITES-STATE | not enforced | `health` |
| [`conflicts`](#conflicts) | READ-ONLY (see caveat) | no | itself |
| [`dashboard`](#dashboard) | TOUCHES-PROCESSES (network server) | no | none |
| [`schedule import`](#schedule-import) | WRITES-STATE | not enforced | none |
| [`schedule refresh`](#schedule-refresh) | WRITES-STATE | not enforced | none |
| [`schedule show`](#schedule-show) | READ-ONLY | no | itself |
| [`schedule notify-tick`](#schedule-notify-tick) | TOUCHES-PROCESSES, WRITES-STATE | yes (delivers into the kid's session) | `--test` |
| [`enforce show`](#enforce-show) | READ-ONLY | root or `timekpr` group (help text) | itself |
| [`enforce plan`](#enforce-plan) | READ-ONLY | root or `timekpr` group (help text) | itself |
| [`enforce apply`](#enforce-apply) | TOUCHES-PROCESSES | root or `timekpr` group (help text) | `enforce plan` |
| [`enforce chargeback`](#enforce-chargeback) | TOUCHES-PROCESSES, WRITES-STATE | yes for a real run | `--dry-run` |
| [`enforce grant`](#enforce-grant) | TOUCHES-PROCESSES, WRITES-STATE | effectively yes | none |
| [`enforce grants`](#enforce-grants) | READ-ONLY | not enforced | itself |
| [`enforce games`](#enforce-games) | READ-ONLY, or WRITES-STATE with `--forget` | yes in practice | itself |
| [`enforce revoke`](#enforce-revoke) | TOUCHES-PROCESSES, WRITES-STATE | effectively yes | none |
| [`enforce web-block`](#enforce-web-block) | TOUCHES-PROCESSES, WRITES-STATE | yes for a real run | `--dry-run` |
| [`extension build`](#extension-build) | WRITES-STATE (files only) | no | none |

Command tree:

```
familymonctl [-v]
  status | capture-once | capture-run | capture-supervise | capture-probe (hidden) | classify | health
         | heartbeat-once | migrate-state | events | retention | personal-time | conflicts | dashboard
  schedule  {import, refresh, show, notify-tick}
  enforce   {show, plan, apply, chargeback, grant, grants, games, revoke, web-block}
  extension {build}
```

## Exit codes

| Command | Code | Meaning |
|---|---|---|
| any | 0 | success |
| any | 1 | runtime error raised as a click exception (standard click behaviour) |
| any | 2 | click usage error: bad option or missing argument (standard click behaviour) |
| `heartbeat-once` | 0 / 0 / 1 / 2 / 3 | ok / idle (kid not logged in) / stale / dead / never |
| `health` | 2 | privacy violation (stderr message) |
| `health` | 4 | DB integrity not `ok` (stderr message) |
| `enforce apply` | 3 | refused: would cut off an active session, no `--force` |

Note that `health` exit 2 and click's usage-error exit 2 share a number; a usage error prints click's usage text instead of the privacy message.

---

## status

Print runtime status.

```
familymonctl status
```

- No options. Root: no. READ-ONLY. It does not open the DB.
- Prints `state_dir`, `db_path` with `exists=`, and `screenshots_dir` with `exists=`.
- Use it to confirm which state directory a shell resolves to before running anything else.

```bash
familymonctl status
```

## capture-once

Run a single capture tick and exit, in your own session. For local sanity checks (dev).

```
familymonctl capture-once
```

- No options. Root: not enforced in code. Class: WRITES-STATE and TOUCHES-PROCESSES.
- Opens the DB, runs one tick with the KWin Wayland backend (needs a KDE/Wayland session; the tick takes a screenshot through `spectacle`), and writes a capture row and possibly a screenshot.
- Output: `id=... active=... shot=... skipped=...`.
- Safety: capture reads the kid's windows and screen. Run it only in the session you mean to capture, and never as a way to peek at the kid's session (messaging windows are redacted by the capture pipeline, not by ad-hoc screenshots).
- Safe preview: none. Use a throwaway `FAMILYMON_STATE_DIR` for experiments.

```bash
FAMILYMON_STATE_DIR=/tmp/fm-test familymonctl capture-once
```

## capture-run

Run the capture loop inside your own graphical session until SIGINT. This is the dev loop; production uses [`capture-supervise`](#capture-supervise).

```
familymonctl capture-run
```

- No options. Root: not enforced in code (run it as yourself, not root: root cannot reach a KWin session). Class: WRITES-STATE and TOUCHES-PROCESSES (long-running). It writes your own dev state dir.
- Interval is `$FAMILYMON_INTERVAL` seconds (default 60).
- The packaged units do not run it. The dev-only user units under `deploy/systemd/user/` do; see [files-and-units.md](files-and-units.md).
- Undo: stop the process.

## capture-supervise

The production capture loop (`familymon-capture.service`).

```
familymonctl capture-supervise
```

- No options. Root: required; refuses otherwise ("capture-supervise runs as root"). Class: WRITES-STATE and TOUCHES-PROCESSES (long-running).
- Needs the monitored user: `$FAMILYMON_USER`, else `user:` in `policy.yaml`; otherwise it exits with an error.
- Every `$FAMILYMON_INTERVAL` seconds (default 60): asks logind about the user's session. No session: the tick is skipped and nothing is recorded. Otherwise it writes a one-time KWin window-list script (nonce, random name, mode 0644) into `/run/familymon/kwin` (dir 0711), runs `capture-probe` as the user through `setpriv --reuid=<uid> --regid=<gid> --init-groups --no-new-privs -- env -i ... python -I -m familymon.cli capture-probe`, deletes the script, then does game detection, pHash dedup and persistence as root.
- If the window list does not arrive no screenshot is taken (skip reason `window_list_unavailable`). A probe failure while a session exists writes a `capture_failed` event; a window-list push rejected by the sink (wrong D-Bus sender or wrong nonce) writes `capture_tamper`.
- Re-scans the Steam library every 30 minutes and remembers games seen installed (`known_games.json`).
- Personal time (`personal_time` in `policy.yaml`, re-read every tick together with the active grants): the probe is asked to skip the screenshot (reason `personal_time`, unless the session is locked or inactive, whose reason wins) and the row is flagged `personal_time` for the dashboard to hide its app and title. See [config.md](config.md) section 3.10. An unusable section is logged once (`personal time: policy unusable`) and monitoring continues in full.
- Safety: the supervisor, not the kid, owns the loop. Stopping it stops monitoring, so tell the parent first. Do not run a second copy by hand next to the unit.
- Undo: `sudo systemctl stop familymon-capture.service` (it restarts on boot while enabled).

## capture-probe

Hidden, internal. Not for operators. The root supervisor starts it as the monitored user once per tick: it reads a request (script path, nonce, suppress reason) as JSON on stdin, takes one in-session capture (window list through KWin, screenshot through `spectacle`, messaging redaction) and prints the already-redacted `CaptureResult` JSON on stdout. It sets itself non-dumpable (`PR_SET_DUMPABLE=0`). It writes no state. Running it by hand only makes sense when debugging the supervisor, inside the kid's session.

## classify

Classify captures that have no classification row yet. Heuristic only: app lookup via `.desktop` scan, domain lookup via bundled YAML, fallback `unknown`. Idempotent.

```
familymonctl classify [--limit INTEGER] [--redo]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--limit` | int | none | Cap the number of rows processed. Ignored with `--redo`. |
| `--redo` | flag | off | Discard all heuristic-sourced classifications and re-run the heuristics over the whole history (after a classification-policy change). VLM and bootstrap labels are kept. |

- Root: no check in code. Operates on `db_path()` of the running user. Class: WRITES-STATE.
- The schedule's group codes are added as `school` brand keywords (reads `schedule.yaml` if present).
- `--redo` deletes only rows whose source is `heuristic`, `domain` or `game_process`, then recomputes. Use it to relabel rows shown as `unknown` after a timer gap or after a policy change.
- Output: `classified N capture(s) (apps=... keys, domains=..., brands=...)` or `re-classified N ...` with `--redo`.
- Parent decides: `--redo` applies the current classification rules to all history. Change rules only after the parent has chosen the policy, not on one ambiguous data point.
- Verify: `familymonctl health` (classified 24h and category lines).
- Undo: none for `--redo` beyond re-running it after reverting the rules.

```bash
familymonctl classify
familymonctl classify --redo
```

## health

Print a data-health report. Exits non-zero on a privacy violation. Run at the start of each iteration, before shipping changes.

```
familymonctl health
```

- No options. Root: yes in practice (the DB is root-owned). READ-ONLY on data, with one caveat: opening the DB runs an integrity check and may auto-recover a damaged DB (damaged file set aside, recovery attempted, a `db_recovered` event written).
- Reads `db_path()` and `screenshots_dir()`.
- Report lines: as of, total captures, last 1h/24h, last capture age, largest gap 24h, classified 24h, dedup 24h (`n/a` when none eligible), msg full-skips24h, screenshots (count and MB), categories 24h.
- Soft warnings on stdout (exit code unchanged): capture stale (last capture older than 180 s), a gap over 300 s in 24h, messaging full-skips over 10% of captures, `! DB was auto-recovered: ...`.
- Exit 2: privacy violation (an un-redacted screenshot exists for a capture with a messaging window present). Checked and exited before the integrity check.
- Exit 4: DB integrity is not `ok`.
- Success lines: `privacy check:    OK (...)` and `db integrity:     ok`.

```bash
familymonctl health; echo "exit=$?"
```

See [../operate/verify-and-health.md](../operate/verify-and-health.md).

## heartbeat-once

Run one capture-freshness check, log a heartbeat event, print status. Driven by a root timer so liveness is observed independently of the capture loop.

```
familymonctl heartbeat-once
```

- No options. Root: needed in practice (as root it asks logind about the monitored user's session; as anyone else it asks about your own, dev only). Class: WRITES-STATE (inserts a `heartbeat` event).
- Status thresholds from last capture age: under 180 s `ok`, 180 to 600 s `stale`, 600 s or more `dead`; no captures `never`. If the monitored user has no graphical session (`none`) and nothing fresh, the status is `idle`.
- Exit code: `ok`=0, `idle`=0, `stale`=1, `dead`=2, `never`=3. A non-zero exit is deliberate: it marks the systemd unit failed. (The dashboard also derives `watcher_down` from an old newest heartbeat; this command never prints it.)
- The event data holds `status`, `session` (`active`, `locked`, `inactive`, `none` or `unknown`), `age_seconds` and `last_capture_at`.
- Output: `heartbeat: <status> (last capture <N>s ago|n/a ago; session <label>)`.
- Safe preview: `familymonctl health` shows the same freshness without writing an event.
- Normally run by `familymon-heartbeat.timer` (root, every minute).

## migrate-state

Move a monitored user's old per-user state into the root state dir. The package's postinst runs it when `policy.yaml` exists.

```
familymonctl migrate-state [--user TEXT]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--user` | text | the monitored user (`$FAMILYMON_USER`, else `user:` in `policy.yaml`) | Account whose `~/.local/state/familymon` to migrate. |

- Root: required. Class: WRITES-STATE. Stop the user's old capture service first (the postinst does).
- Copies the old `captures.db` with the SQLite backup API (screenshot paths rewritten, root's own events and any captures root took meanwhile appended), moves the screenshots, copies `schedule.yaml`, `notified.json` and `warned.json` only where root has none, then moves the leftover directory to `/var/lib/familymon/migrated/<user>-<timestamp>/` (root-only backup; nothing is deleted). Writes the marker `/var/lib/familymon/.migrated-from-<user>`.
- Idempotent and resumable: on failure it says "migration failed (safe to re-run)". Refuses a symlinked state dir and never follows symlinks inside it.
- Output: `<user>: nothing to migrate`, or a one-line summary of what moved and where the backup is.
- Undo: restore from the backup directory by hand; there is no automatic reverse.

## retention

Delete screenshots older than the retention age. The daily `familymon-retention.timer` runs it.

```
familymonctl retention [--dry-run] [--days N]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--dry-run` | flag | off | Report what would be deleted; change nothing. |
| `--days` | int | `retention.screenshots_days` from `policy.yaml`, else 90 | Override the age; `0` deletes nothing. |

- Class: WRITES-STATE (deletes files). Root in practice: the state dir is root-owned.
- Prunes whole day directories under `<state>/screenshots/` whose date is older than the age (the directory name decides, not file timestamps), clears `screenshot_path` on the affected `capture` rows, removes emptied directories, and records one `screenshots_pruned` event when anything was deleted. Capture rows and classifications are never deleted. Symlinks, non-date directories and anything that is not a regular file are left alone.
- Output: `deleted N screenshot(s), X MB, from D day(s) (first to last); keeping 90 days (days before YYYY-MM-DD)`, and `cleared the screenshot path on N capture row(s)` when rows changed. With `--dry-run` the verbs are `would delete` and `would clear`.
- Exit: 0. A policy value that is not a whole number of days is a usage error (exit 1).
- Verify: `sudo familymonctl retention --dry-run` (reports 0 afterwards), `sudo familymonctl health` (screenshot count and size), `sudo familymonctl events --kind screenshots_pruned`.

## personal-time

Show today's personal-time window (`personal_time` in `policy.yaml`, with active grants folded in) and what it covered.

```
familymonctl personal-time [--day YYYY-MM-DD]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--day` | date | today | Day to count hidden captures for. The window shown is always today's. |

- Root: yes in practice (reads `policy.yaml`, `grants.json` and the DB, read-only). Class: READ-ONLY.
- Output: `personal time today: 20:00–22:00 (ends at the bedtime lock)` or `personal time today: none`; then `now HH:MM: in personal time (...)` or `now HH:MM: outside personal time (full capture)`; then, when a DB exists, `hidden captures on <day>: N (games: G, about M min)`. The minutes are `G` times `$FAMILYMON_INTERVAL`.
- An unusable section exits 1 with `personal_time in policy.yaml is unusable: ...`. The capture supervisor then monitors in full and logs the same.

## events

List the day's operational events from the capture DB.

```
familymonctl events [--day YYYY-MM-DD] [--kind TEXT]...
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--day` | text | today | Day to list. An invalid value raises a ValueError traceback. |
| `--kind` | text, repeatable | all kinds except `heartbeat` | Only these kinds, e.g. `--kind capture_failed --kind capture_tamper`. Give `--kind heartbeat` to see heartbeats. |

- Root: needed in practice (the DB is not readable by the kid). READ-ONLY: opens the DB with the read-only engine, so it never triggers recovery.
- Output: one line per event, `HH:MM:SS  <kind>  <data as JSON>`, then `N event(s) on <date>`. A missing DB is an error.
- Kinds you may see: `capture_failed`, `capture_tamper`, `heartbeat`, `db_recovered`, `playtime_chargeback`, `grant_hours`, `grant_playtime`, `web_block`, `lesson_close` (see [files-and-units.md](files-and-units.md)).

```bash
sudo familymonctl events --day 2026-10-19
sudo familymonctl events --kind capture_failed --kind capture_tamper
```

## conflicts

List leisure-during-lesson conflicts for a day.

```
familymonctl conflicts [--day TEXT]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--day` | text `YYYY-MM-DD` | today | Day to evaluate. An invalid value raises a ValueError traceback. |

- Root: yes in practice. READ-ONLY (same DB-open caveat as `health`). Reads `schedule.yaml` and the DB in `/var/lib/familymon`.
- No schedule: prints `no schedule imported — run 'familymonctl schedule import' first` and exits 0.
- Joins captures with classifications for the day and estimates duration from `FAMILYMON_INTERVAL`.
- Output: `<date>: no conflicts` or `<date>: N conflict(s)` followed by one line per episode.

```bash
familymonctl conflicts --day 2026-01-15
```

## dashboard

Start the FastAPI dashboard. Requires `FAMILYMON_DASH_USER` and `FAMILYMON_DASH_PASS`.

```
familymonctl dashboard [--bind TEXT] [--port INTEGER]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--bind` | text | `$FAMILYMON_DASH_BIND`, else `127.0.0.1` | Interface to bind. Use `0.0.0.0` for LAN exposure. |
| `--port` | int | `$FAMILYMON_DASH_PORT`, else `8765` | Port. `--port 0` is treated as unset. |

- Root: no, and in production it deliberately does not run as root: the unit runs it as the `familymon` user with `FAMILYMON_STATE_DIR=/var/lib/familymon`. Class: TOUCHES-PROCESSES (long-running network server). It serves the state dir's DB (opened read-only) and `schedule.yaml`. The banner reads the newest heartbeat event and a red line summarises the day's `capture_failed` and `capture_tamper` events.
- Auth: HTTP Basic, one shared user and password from the two env vars. The app raises `RuntimeError` at startup if either is unset or empty.
- No TLS is configured. The bind address is the only exposure control.
- Routes (all authenticated): `GET /`, `GET /d/{YYYY-MM-DD}`, `GET /api/captures?day=`, `GET /api/stats?day=`. Metadata only; no screenshot bytes are served.
- Prints `familymon dashboard binding HOST:PORT`, then runs uvicorn.
- Safety: binding beyond `127.0.0.1` exposes the kid's activity timeline over plain HTTP to the network. Parent decides.
- Normally run by the system unit `familymon-dashboard.service`, which reads `/etc/familymon/dashboard.env` (root:root 0600) and is skipped if that file is missing.

```bash
FAMILYMON_DASH_USER=parent FAMILYMON_DASH_PASS='change-me' familymonctl dashboard --port 8765
```

---

## schedule

View and import the school timetable. See also [config.md](config.md) for `schedule_extras.yaml`.

### schedule import

Import a timetable export into the schedule (overwrites the current one). For Kings InterHigh: save the JSON from `GET /student/api/timetable/week` and pass the file.

```
familymonctl schedule import [--format [kingsinterhigh]] SOURCE
```

| Argument / option | Type | Default | Meaning |
|---|---|---|---|
| `SOURCE` | existing file | required | The saved JSON export. |
| `--format` | choice `kingsinterhigh` | `kingsinterhigh` | Timetable export format. It is the only format. |

- Root: yes in practice. Class: WRITES-STATE: overwrites `/var/lib/familymon/schedule.yaml`.
- Reads `schedule_extras.yaml` (excluded subjects, extra blocks, breaks) and applies it over the parsed lessons.
- Output: `imported N block(s) -> path (timezone: ...; X school, Y extra, excluded subjects: ...|none; Z break(s))`, then one `  break: LABEL (cats blocks suspended)` line per break, with `[in progress — no lessons today]` when a break covers today.
- Safety: copy the old file first (`sudo cp /var/lib/familymon/schedule.yaml{,.bak}`); the import overwrites it. Every consumer reads this one file.
- Undo: re-import the previous export. There is no dry-run.

```bash
sudo familymonctl schedule import ~/Downloads/week.json
```

### schedule refresh

Re-apply the parent extras to the current schedule without an export. Imported lessons are kept as they are; exclusions, custom blocks and breaks are rebuilt from the extras file.

```
familymonctl schedule refresh
```

- No options. Root: yes in practice. Class: WRITES-STATE (rewrites `/var/lib/familymon/schedule.yaml`).
- Keeps only blocks with category `school` and the timezone, then reapplies extras.
- No schedule: ClickException, exit 1 (`no schedule at ... — run 'familymonctl schedule import' first`).
- Output: `refreshed N block(s) -> path (...)` plus break lines.
- Use after editing `/etc/familymon/schedule_extras.yaml`.

### schedule show

Print the current weekly schedule.

```
familymonctl schedule show
```

- No options. Root: yes in practice (the file is in `/var/lib/familymon`). READ-ONLY.
- No schedule: prints a hint and exits 0 (not an error).
- Output: blocks grouped by day as `HH:MM-HH:MM  category subject [notify]`, then `N block(s) total` and breaks.

### schedule notify-tick

Pop a desktop notification for notify-flagged blocks starting now, and a warning when a distraction is seen during the lesson in progress. Runs every minute from `familymon-notify.timer` as root and delivers into the monitored user's session (through `runuser` and their session bus). Per-day watermark files prevent repeats.

```
familymonctl schedule notify-tick [--test] [--no-warn] [--warn-cooldown INTEGER]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--test` | flag | off | Send a sample notification now and exit. Reads no schedule. |
| `--no-warn` | flag | off | Skip the live lesson-conflict warning. |
| `--warn-cooldown` | int (minutes) | 5 | Minutes between repeated warnings for the same lesson and category. |

- Root: yes in production (it reads root state and delivers into the kid's session). A non-root dev run notifies into your own session. Class: TOUCHES-PROCESSES (notifications) and WRITES-STATE (`notified.json`, `warned.json` in the state directory; each is updated only when the notification or warning was actually delivered, so one that could not be delivered is retried on the next tick).
- Personal time: while the policy's `personal_time` window is in effect, one notice per day ("Personal time", with the end time, that no screenshots are taken, that app names and titles are hidden, and that game time still counts). Marked in `personal_time_notified.json` only when delivered, so a kid who logs in later in the window still gets it. Prints `notified: personal time`. Runs before the schedule is read, so it works without a schedule.
- No schedule: returns silently, exit 0.
- Block-start notifications: one per due block per day; prints `notified: N`. With no session to notify, nothing is marked done.
- Live warning: only while a `school` block is in progress and a DB exists; looks at the last 3 minutes of captures; prints `warned: <category> during <subject>`.
- Safe preview: `--test` sends one harmless notification. Verify delivery through the desktop's notification history or D-Bus state, never a raw screenshot of the kid's session.

With `--test` the command prints `test notification sent`, or `no session to notify` when the monitored user is not logged in.

```bash
sudo familymonctl schedule notify-tick --test
```

---

## enforce

Drive timekpr-next from the enforcement policy (`policy.yaml`). Runs on the machine timekpr enforces and needs root or the `timekpr` group (stated in help text; the code does not check the uid except for process closing in `web-block`). Live state is read over D-Bus because timekpr 0.5.10's CLI misprints booleans; changes are written by running the `timekpra` command-line tool (so it must be installed and on `PATH`).

All commands that compute a desired state first:

1. Read `policy.yaml` from the state directory. Missing: `no policy at <path> — copy familymon/defaults/policy_example.yaml (installed: /usr/share/familymon/policy_example.yaml) there first`.
2. Resolve the policy `user` with the system user database. Unknown user: ClickException.
3. Scan that user's Steam library and build the timekpr settings.
4. Fold in today's active grants from `grants.json`.

Config keys are documented in [config.md](config.md). Gotchas for the timekpr backend are in [../operate/troubleshoot.md](../operate/troubleshoot.md).

Parent decides: every value in `policy.yaml` is policy. Agents apply what the parent chose and present trade-offs; they do not change limits on inference.

### enforce show

Print the live timekpr state for USER (read over D-Bus).

```
familymonctl enforce show USER
```

- `USER`: required. Does not read `policy.yaml`. Root or `timekpr` group (help text). READ-ONLY.
- Output: user, active sessions, lockout, allowed weekdays; per day mon to sun the allowed-hours span and `limit=Ns`; whether playtime is enabled and, if so, the playtime limits and `activity: mask [description]` lines.

```bash
sudo familymonctl enforce show kid
```

### enforce plan

Dry run: show what `apply` would change (no writes).

```
familymonctl enforce plan
```

- No options. READ-ONLY. Reads `policy.yaml`, the Steam library, `grants.json` and live D-Bus state.
- Nothing to change: `<user>: live state matches policy — nothing to do`.
- Otherwise: `<user>: N change(s) needed` and per change `<setting>: live: ... desired: ...`.
- If applying now would be unsafe it prints `!! applying NOW would be unsafe:` with reasons and a note that `apply` refuses without `--force`. Exit code stays 0.
- Always run this before `apply`.

```bash
sudo familymonctl enforce plan
```

### enforce apply

Apply the policy to the live timekpr daemon (minimal diff).

```
familymonctl enforce apply [--force]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--force` | flag | off | Apply even if it would cut off an active session right now. |

- Root or `timekpr` group (help text). Class: TOUCHES-PROCESSES (changes are made by running `timekpra`, one call per change, aborting on the first failure). Writes no familymon state and no audit event.
- No changes: prints `nothing to do`, exit 0.
- Unsafe without `--force`: prints reasons to stderr and exits 3 (`refusing to apply — it would cut off an active session`).
- Otherwise prints the changes, applies them and prints `applied N change(s) to <user>`.
- Safety: narrowing allowed hours to exclude the current hour while the kid is logged in ends the session. Do not pass `--force` unless the parent said so; otherwise wait for logout. `familymon-enforce.service` runs this hourly and will show failed on exit 3.
- Preview: `enforce plan`. Verify: `enforce show USER` after a few seconds (timekpr may apply some changes on its own polling interval; the interval is not stated in this repo's code). Undo: edit `policy.yaml` and apply again.
- For a one-off exception use [`enforce grant`](#enforce-grant), never a raw `timekpra --setallowedhours` (persistent, and the reconciler reverts it).

```bash
sudo familymonctl enforce plan && sudo familymonctl enforce apply
```

(Run these as two separate steps and check the plan output before applying.)

### enforce chargeback

Charge browser-game minutes to the user's timekpr PlayTime budget. One reconcile tick (run from `familymon-chargeback.timer` every 5 minutes).

```
familymonctl enforce chargeback [--dry-run]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--dry-run` | flag | off | Report what would be charged; change nothing. |

- Root for a real run (`timekpra` needs root or the `timekpr` group; the watermark is in the root state directory). Class: TOUCHES-PROCESSES and WRITES-STATE: subtracts from the timekpr PlayTime counter (`timekpra --setplaytimeleft`), writes `playtime_charges.json`, writes a `playtime_chargeback` event to the root DB. `--dry-run` is READ-ONLY.
- Reads `/var/lib/familymon/captures.db` strictly read-only. If the stored watermark is above the DB's newest capture id (the DB was replaced, recovered or migrated), the watermark is reset for today instead of silently never charging again.
- Policy keys: `games.enabled` and `games.charge_browser_games` must both be true, otherwise it prints `chargeback disabled (needs games.enabled and games.charge_browser_games)` and exits 0. `games.charge_browser_games_when` is `visible` (default) or `focused`; any other value is an error. The per-run cap is 1800 s.
- No capture DB yet: prints `<user>: no capture DB at ... yet — nothing to charge`, exit 0.
- Output (dry run): `<user>: [when] N uncharged browser-game capture(s) (~Ns); charged today so far Ns`. Real run: `<user>: charged Ns for N browser-game capture(s)` (suffix ` (capped; more next tick)`), or `<user>: nothing new to charge`.
- Parent decides: whether browser games count against play time, and whether `visible` or `focused` windows count.

```bash
sudo familymonctl enforce chargeback --dry-run
```

### enforce grant

One-off grant for today, applied immediately and honored by the reconciler. `--until` widens today's allowed-hours window (expires on its own, never reverted by `familymon-enforce.timer`); `--playtime` adds game minutes.

```
familymonctl enforce grant [--until HH:MM] [--from HH:MM] [--playtime DURATION] [--note TEXT] USER
```

| Argument / option | Type | Default | Meaning |
|---|---|---|---|
| `USER` | text | required | Account to grant to. Must equal the policy `user` for `--until`. |
| `--until` | `HH:MM` | none | Extend today's allowed hours until this time (inclusive). |
| `--from` | `HH:MM` | now | Grant start. |
| `--playtime` | duration | none | Extra game minutes today, e.g. `20m` or `1h` (counter adjust). |
| `--note` | text | empty | Why; kept with the grant. |

- Root: effectively yes (writes `grants.json` and the DB, and calls timekpr). Class: TOUCHES-PROCESSES and WRITES-STATE. No dry-run and no `--force`.
- At least one of `--until` or `--playtime` is required, otherwise ClickException. Both may be given; `--until` is processed first.
- Durations: `2h30m`, `45m`, `90s`, or an integer or digits (seconds). An invalid duration raises a `PolicyError` traceback.
- `--until` path: stores the grant, recomputes the desired state, applies the timekpr changes with no safety guard (grants only widen), writes a `grant_hours` event. If the policy user differs from `USER` it errors after the grant was stored. Output: `<user>: allowed until END today (START-END); N timekpr change(s) applied`.
- `--playtime` path: adds seconds to the timekpr counter (`timekpra --setplaytimeleft`) and writes a `grant_playtime` event. Output: `<user>: +Ns game time today`.
- Parent decides: every grant. Do not grant on the kid's request alone.
- Verify: [`enforce grants`](#enforce-grants), then `enforce show USER`. Undo: [`enforce revoke`](#enforce-revoke) (hours only; a playtime grant is a counter adjustment and has no revoke).

```bash
sudo familymonctl enforce grant kid --until 21:30 --note "family event"
sudo familymonctl enforce grant kid --playtime 30m --note "weekend"
```

### enforce grants

List today's active one-off grants.

```
familymonctl enforce grants
```

- No options. READ-ONLY. Reads `grants.json` from the resolved state directory (use the same user, usually root, that created the grants).
- Output: `no active grants`, or one line per grant: `  user: day start-end  note`.

### enforce revoke

Drop today's hour grants for USER and converge back to policy (guarded).

```
familymonctl enforce revoke USER
```

- `USER`: required. No `--force`. Root: effectively yes. Class: TOUCHES-PROCESSES and WRITES-STATE (`grants.json`, timekpr). No event is written.
- If narrowing would cut off an active session it removes the grants but leaves timekpr unchanged and prints `revoked N grant(s); timekpr NOT narrowed now (would cut the active session) — the reconciler converges once safe`; exit 0.
- Otherwise prints `revoked N grant(s); M timekpr change(s) applied`.
- Does not check that `USER` matches the policy `user`; use the policy user.

```bash
sudo familymonctl enforce revoke kid
```

### enforce games

List the game directories familymon budgets: the ones remembered in `known_games.json` plus whatever is installed now.

```
familymonctl enforce games [--forget DIR]...
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--forget` | directory, repeatable | none | Stop treating this remembered game dir as a game. |

- Root: needed in practice (`known_games.json` is root-only). Without `--forget` it is READ-ONLY and prints one line per dir, adding `(not installed now)` for dirs that no longer exist.
- A game seen installed stays on the list after it disappears from the Steam library, so hiding a game's files does not remove its PlayTime mask. Only the parent removes one, with `--forget`. A dir that is still installed is re-added on the next scan. A dir that is not remembered is an error.
- The next `enforce apply` (hourly) updates timekpr's masks.
- Parent decides which games stay on the budget.

```bash
sudo familymonctl enforce games
sudo familymonctl enforce games --forget /home/kid/.local/share/Steam/steamapps/common/OldGame
```

### enforce web-block

Reconcile Chrome's managed policy to lesson-time web blocking. One tick (`familymon-webblock.timer`, every minute): while a school block is in progress, `URLBlocklist` is the configured distraction domains; otherwise the key is dropped. Idempotent; writes only on change.

```
familymonctl enforce web-block [--dry-run] [--schedule FILE]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--dry-run` | flag | off | Show the desired Chrome policy; write nothing and kill nothing. |
| `--schedule` | existing file | `/var/lib/familymon/schedule.yaml` | Schedule YAML to evaluate. |

- Needs `web_block.enabled: true` and a string `user` in `policy.yaml`. A bad `web_block` config is an error.
- Root for a real run. Class: TOUCHES-PROCESSES and WRITES-STATE. Writes `/etc/opt/chrome/policies/managed/familymon.json` (a write failure for lack of permission says to run as root), writes `web_block` and `lesson_close` events to the root DB, may send the user a notification, and may close forbidden processes.
- The process sweep scans whenever web-block is enabled, but closes processes only while a lesson is in progress and only as root. It refuses uid 0. Matching processes of the policy user get SIGTERM, then SIGKILL after a 3 s grace.
- No schedule file and web-block enabled: prints a warning that nothing will be blocked until one is imported.
- While `web_block.enabled` is true the file always holds the base keys `DnsOverHttpsMode: "off"`, `EncryptedClientHelloEnabled: false`, `DeveloperToolsAvailability: 0`, plus `ExtensionInstallForcelist` and a `3rdparty` entry for the extension (from [`extension build`](#extension-build)) when the build ships one. `web_block.block_private` is `lessons` (default), `always` or `off`; when private windows are blocked (`always`, or `lessons` during a lesson) the keys `IncognitoModeAvailability: 1` and `BrowserGuestModeEnabled: false` are added. During a lesson the document also gets `URLBlocklist` and, if there are allowed domains, `URLAllowlist`. Default blocked categories: `messaging`, `social`, `games`. Never blocked: `meet.google.com`, `teams.microsoft.com`, `zoom.us`.
- Dry-run output: the plan description, a school-break line if any, and when a document exists the extension, private-window setting, policy file path (exists or absent), block set, the desired JSON and `change needed: yes|no`; then, when enabled, `forbidden apps running now (closed only during a lesson): ...`.
- Real output: `converged: ...`, `updated: ...`, or `closed during <subject>: names (N process(es))`.
- Safety: this can kill the kid's browser mid-lesson. Chrome's URLBlocklist only applies on navigation; already-open tabs are closed by the extension. Chrome started with `--disable-extensions` runs without it, so the sweep closes it. Dry-run still scans processes (read-only).
- Verify: `--dry-run`, then check the policy file. Undo: set `web_block.enabled: false` in `policy.yaml`; the next real run as root deletes `/etc/opt/chrome/policies/managed/familymon.json` entirely (base keys included) and records a `web_block` event. Disabled mode also skips the process sweep.

```bash
sudo familymonctl enforce web-block --dry-run
```

---

## extension

### extension build

Sign and package the Chrome lesson-block extension as a CRX3 plus its update manifest. Normally run by `scripts/build-deb.sh` on the dev machine; the web-block tick force-installs the result through Chrome policy.

```
familymonctl extension build --key FILE --out DIRECTORY [--version TEXT] [--src DIRECTORY] [--deployed-dir TEXT]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--key` | file | required | RSA private key (PEM) that signs the CRX. Created if missing. |
| `--out` | directory | required | Output directory for `familymon.crx`, `update.xml`, `id`, `version`. |
| `--version` | text | `1.<yymm>.<d>.<hmm>` from now | Extension version. Must increase between builds or Chrome keeps the old one. |
| `--src` | existing directory | bundled `familymon/chrome_extension` | Extension source. |
| `--deployed-dir` | text | `/usr/share/familymon/chrome-extension` | Where the deb installs the output; baked into `update.xml` as `file://` URLs. |

- Root: no. Class: WRITES-STATE (files only; never the DB or timekpr). Needs the `openssl` CLI.
- A missing key is generated (RSA 2048, mode 0600) with a message. The key determines the extension id; a lost key only changes the id.
- Output: `extension <id> v<version> -> <out_dir> (update url ...)`.
- Safety: keep the signing key private and out of version control.

```bash
familymonctl extension build --key private/chrome-extension-key.pem --out /tmp/fm-ext
```

---

## Related

- Units that run these commands on a schedule: [files-and-units.md](files-and-units.md)
- Policy and config keys: [config.md](config.md)
- Health checks: [../operate/verify-and-health.md](../operate/verify-and-health.md)
- Failure modes: [../operate/troubleshoot.md](../operate/troubleshoot.md)
- Testing and shipping: [../develop/testing-and-shipping.md](../develop/testing-and-shipping.md)
