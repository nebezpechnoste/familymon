# familymonctl CLI reference

Read this when you need the exact synopsis, options, root requirement, side effects or exit codes of a `familymonctl` command, or when you must decide whether a command is safe to run against a live system. For concepts see [concepts.md](../concepts.md). For paths, units and config keys see [config.md](config.md) and [files-and-units.md](files-and-units.md). For routine checks see [../operate/verify-and-health.md](../operate/verify-and-health.md); for failures see [../operate/troubleshoot.md](../operate/troubleshoot.md).

Side-effect classes used below:

| Class | Meaning |
|---|---|
| READ-ONLY | Reads files, DB or D-Bus state. Changes nothing (see caveats per command). |
| WRITES-STATE | Writes familymon files, DB rows or events. |
| TOUCHES-PROCESSES | Changes timekpr, Chrome policy, notifications, kills processes, or runs a long-lived process or server. |

"Root?" is what the code or help text requires. Most commands do not check the effective uid in code; where root is only stated in help text, this is noted.

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

1. `$FAMILYMON_STATE_DIR`
2. `/var/lib/familymon` when the effective uid is 0
3. `${XDG_STATE_HOME:-~/.local/state}/familymon`

Under it: `captures.db`, `screenshots/`, `schedule.yaml`, `policy.yaml`. The `enforce` commands also use `grants.json` and `playtime_charges.json`; `schedule notify-tick` uses `notified.json` and `warned.json`.

Root-side commands that read the kid's data use `<home of kid>/.local/state/familymon` (they ignore `FAMILYMON_STATE_DIR` and `XDG_STATE_HOME` for that).

Config files (`schedule_extras.yaml`, `domains_starter.yaml`, ...) resolve in order: `$FAMILYMON_CONFIG_DIR/<name>`, `/etc/familymon/<name>`, bundled defaults.

| Variable | Default | Used by |
|---|---|---|
| `FAMILYMON_STATE_DIR` | see above | all |
| `FAMILYMON_CONFIG_DIR` | unset | config lookup |
| `FAMILYMON_INTERVAL` | `60` (seconds) | `capture-run`, `conflicts` (duration estimate) |
| `FAMILYMON_DASH_USER`, `FAMILYMON_DASH_PASS` | none, both required | `dashboard` |
| `FAMILYMON_DASH_BIND` | `127.0.0.1` | `dashboard` |
| `FAMILYMON_DASH_PORT` | `8765` | `dashboard` |

Safety: a non-root run uses `~/.local/state/familymon`, which is a separate DB from `/var/lib/familymon`. Do not run write commands as the wrong user and assume they hit the production state.

## Index

| Command | Class | Root? | Safe preview |
|---|---|---|---|
| [`status`](#status) | READ-ONLY | no | itself |
| [`capture-once`](#capture-once) | WRITES-STATE, TOUCHES-PROCESSES | not enforced | none |
| [`capture-run`](#capture-run) | WRITES-STATE, TOUCHES-PROCESSES | not enforced | none |
| [`classify`](#classify) | WRITES-STATE | not enforced | none |
| [`health`](#health) | READ-ONLY (see caveat) | no | itself |
| [`heartbeat-once`](#heartbeat-once) | WRITES-STATE | not enforced | `health` |
| [`conflicts`](#conflicts) | READ-ONLY (see caveat) | no | itself |
| [`dashboard`](#dashboard) | TOUCHES-PROCESSES (network server) | no | none |
| [`schedule import`](#schedule-import) | WRITES-STATE | not enforced | none |
| [`schedule refresh`](#schedule-refresh) | WRITES-STATE | not enforced | none |
| [`schedule show`](#schedule-show) | READ-ONLY | no | itself |
| [`schedule notify-tick`](#schedule-notify-tick) | TOUCHES-PROCESSES, WRITES-STATE | no (needs the kid's session bus) | none |
| [`enforce show`](#enforce-show) | READ-ONLY | root or `timekpr` group (help text) | itself |
| [`enforce plan`](#enforce-plan) | READ-ONLY | root or `timekpr` group (help text) | itself |
| [`enforce apply`](#enforce-apply) | TOUCHES-PROCESSES | root or `timekpr` group (help text) | `enforce plan` |
| [`enforce chargeback`](#enforce-chargeback) | TOUCHES-PROCESSES, WRITES-STATE | yes for a real run | `--dry-run` |
| [`enforce grant`](#enforce-grant) | TOUCHES-PROCESSES, WRITES-STATE | effectively yes | none |
| [`enforce grants`](#enforce-grants) | READ-ONLY | not enforced | itself |
| [`enforce revoke`](#enforce-revoke) | TOUCHES-PROCESSES, WRITES-STATE | effectively yes | none |
| [`enforce web-block`](#enforce-web-block) | TOUCHES-PROCESSES, WRITES-STATE | yes for a real run | `--dry-run` |
| [`extension build`](#extension-build) | WRITES-STATE (files only) | no | none |

Command tree:

```
familymonctl [-v]
  status | capture-once | capture-run | classify | health | heartbeat-once | conflicts | dashboard
  schedule  {import, refresh, show, notify-tick}
  enforce   {show, plan, apply, chargeback, grant, grants, revoke, web-block}
  extension {build}
```

## Exit codes

| Command | Code | Meaning |
|---|---|---|
| any | 0 | success |
| any | 1 | runtime error raised as a click exception (standard click behaviour) |
| any | 2 | click usage error: bad option or missing argument (standard click behaviour) |
| `heartbeat-once` | 0 / 1 / 2 / 3 | ok / stale / dead / never |
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

Run a single capture tick and exit. For local sanity checks.

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

Run the capture loop until SIGINT.

```
familymonctl capture-run
```

- No options. Root: not enforced in code. Class: WRITES-STATE and TOUCHES-PROCESSES (long-running).
- Interval is `$FAMILYMON_INTERVAL` seconds (default 60).
- Scans the Steam game library once at startup; restart to re-scan.
- Normally run by the user unit `familymon-capture.service` inside the monitored user's graphical session, not by hand. See [files-and-units.md](files-and-units.md).
- Undo: stop the process or the unit.

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

- No options. Root: no. READ-ONLY on data, with one caveat: opening the DB runs an integrity check and may auto-recover a damaged DB (damaged file set aside, recovery attempted, a `db_recovered` event written).
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

Run one capture-freshness check, log a heartbeat event, print status. Drive from a timer so liveness is observed independently of the capture loop.

```
familymonctl heartbeat-once
```

- No options. Root: not enforced in code. Class: WRITES-STATE (inserts a `heartbeat` event).
- Status thresholds from last capture age: under 180 s `ok`, 180 to 600 s `stale`, 600 s or more `dead`; no captures `never`.
- Exit code: `ok`=0, `stale`=1, `dead`=2, `never`=3. A non-zero exit is deliberate: it marks the systemd unit failed.
- Output: `heartbeat: <status> (last capture <N>s ago|n/a ago)`.
- Safe preview: `familymonctl health` shows the same freshness without writing an event.
- Normally run by `familymon-heartbeat.timer` (user scope).

## conflicts

List leisure-during-lesson conflicts for a day.

```
familymonctl conflicts [--day TEXT]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--day` | text `YYYY-MM-DD` | today | Day to evaluate. An invalid value raises a ValueError traceback. |

- Root: no. READ-ONLY (same DB-open caveat as `health`). Reads `schedule.yaml` and the DB.
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

- Root: no. Class: TOUCHES-PROCESSES (long-running network server). It serves the running user's own DB and `schedule.yaml`.
- Auth: HTTP Basic, one shared user and password from the two env vars. The app raises `RuntimeError` at startup if either is unset or empty.
- No TLS is configured. The bind address is the only exposure control.
- Routes (all authenticated): `GET /`, `GET /d/{YYYY-MM-DD}`, `GET /api/captures?day=`, `GET /api/stats?day=`. Metadata only; no screenshot bytes are served.
- Prints `familymon dashboard binding HOST:PORT`, then runs uvicorn.
- Safety: binding beyond `127.0.0.1` exposes the kid's activity timeline over plain HTTP to the network. Parent decides.
- Normally run by the user unit `familymon-dashboard.service`, which reads an optional `/etc/familymon/dashboard.env`.

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

- Root: no check in code. Class: WRITES-STATE: overwrites `schedule.yaml` in the resolved state directory.
- Reads `schedule_extras.yaml` (excluded subjects, extra blocks, breaks) and applies it over the parsed lessons.
- Output: `imported N block(s) -> path (timezone: ...; X school, Y extra, excluded subjects: ...|none; Z break(s))`, then one `  break: LABEL (cats blocks suspended)` line per break, with `[in progress — no lessons today]` when a break covers today.
- Safety: run as the user whose schedule is meant (the kid's state directory, since root-side jobs read `~kid/.local/state/familymon/schedule.yaml`). A root run would write `/var/lib/familymon/schedule.yaml`, which `enforce web-block` does not read by default.
- Undo: re-import the previous export. There is no dry-run.

```bash
familymonctl schedule import ~/Downloads/week.json
```

### schedule refresh

Re-apply the parent extras to the current schedule without an export. Imported lessons are kept as they are; exclusions, custom blocks and breaks are rebuilt from the extras file.

```
familymonctl schedule refresh
```

- No options. Root: no check in code. Class: WRITES-STATE (rewrites `schedule.yaml`).
- Keeps only blocks with category `school` and the timezone, then reapplies extras.
- No schedule: ClickException, exit 1 (`no schedule at ... — run 'familymonctl schedule import' first`).
- Output: `refreshed N block(s) -> path (...)` plus break lines.
- Use after editing `/etc/familymon/schedule_extras.yaml`.

### schedule show

Print the current weekly schedule.

```
familymonctl schedule show
```

- No options. Root: no. READ-ONLY.
- No schedule: prints a hint and exits 0 (not an error).
- Output: blocks grouped by day as `HH:MM-HH:MM  category subject [notify]`, then `N block(s) total` and breaks.

### schedule notify-tick

Pop a desktop notification for notify-flagged blocks starting now, and a warning when a distraction is seen during the lesson in progress. Runs every minute from `familymon-notify.timer` inside the kid's session. Per-day watermark files prevent repeats.

```
familymonctl schedule notify-tick [--test] [--no-warn] [--warn-cooldown INTEGER]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--test` | flag | off | Send a sample notification now and exit. Reads no schedule. |
| `--no-warn` | flag | off | Skip the live lesson-conflict warning. |
| `--warn-cooldown` | int (minutes) | 5 | Minutes between repeated warnings for the same lesson and category. |

- Root: no. It must run inside the kid's session (needs the session bus for `notify-send`). Class: TOUCHES-PROCESSES (notifications) and WRITES-STATE (`notified.json`, `warned.json` in the state directory, the state directory is created with mode 0700 if missing; `notified.json` is written only when a notification fires, `warned.json` only when a warning fires).
- No schedule: returns silently, exit 0.
- Block-start notifications: one per due block per day; prints `notified: N`.
- Live warning: only while a `school` block is in progress and a DB exists; looks at the last 3 minutes of captures; prints `warned: <category> during <subject>`.
- Safe preview: `--test` sends one harmless notification. Verify delivery through the desktop's notification history or D-Bus state, never a raw screenshot of the kid's session.

```bash
familymonctl schedule notify-tick --test
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
- Reads the kid's capture DB at `<home of kid>/.local/state/familymon/captures.db` strictly read-only.
- Policy keys: `games.enabled` and `games.charge_browser_games` must both be true, otherwise it prints `chargeback disabled (needs games.enabled and games.charge_browser_games)` and exits 0. `games.charge_browser_games_when` is `visible` (default) or `focused`; any other value is an error. The per-run cap is 1800 s.
- No kid DB yet: prints `<user>: no capture DB at ... yet — nothing to charge`, exit 0.
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

### enforce web-block

Reconcile Chrome's managed policy to lesson-time web blocking. One tick (`familymon-webblock.timer`, every minute): while a school block is in progress, `URLBlocklist` is the configured distraction domains; otherwise the key is dropped. Idempotent; writes only on change.

```
familymonctl enforce web-block [--dry-run] [--schedule FILE]
```

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--dry-run` | flag | off | Show the desired Chrome policy; write nothing and kill nothing. |
| `--schedule` | existing file | the policy user's `~/.local/state/familymon/schedule.yaml` | Schedule YAML to evaluate. |

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
