# Architecture

Read this when you need to change familymon's code, or need to know which component runs as which user, where data flows, and which invariants must not be broken. For commands see [../reference/cli.md](../reference/cli.md). For paths and units see [../reference/files-and-units.md](../reference/files-and-units.md). For terms see [../concepts.md](../concepts.md). For tests and releases see [testing-and-shipping.md](testing-and-shipping.md).

Familymon is local-first. All data stays on the monitored laptop (`kid-laptop`). Monitoring is disclosed. The parent decides policy.

## Package map

CLI entry point: `familymonctl = "familymon.cli:main"` (`pyproject.toml`). Everything is under `familymon/`.

| Module | Role |
|---|---|
| `cli.py` | All `familymonctl` commands (capture, capture-supervise, capture-probe, classify, health, heartbeat, migrate-state, events, schedule, conflicts, enforce, extension, dashboard, plus `policy`, `config`, `personal-time`, `mcp` and `--json` output on several read commands). |
| `config.py` | `state_dir()`, `monitored_user()` (`FAMILYMON_USER`, else `user:` in `policy.yaml`), `user_state_dir(user)` (only for `migrate-state`), DB/screenshot/schedule/policy paths, `config_file(name)` override order, `capture_interval_seconds()`. |
| `ops.py` | The operations layer: one function per operator operation (status, health, events, timeline, schedule, policy get/validate/set, config get/set, enforce plan/apply/show, grants, games), returning plain data. The CLI commands and the MCP tools both call these, so the two surfaces cannot drift. |
| `mcp_server.py` | The MCP server (`familymonctl mcp`): stdio transport, 21 typed tools over `ops.py`, each with read-only / destructive annotations. Needs the optional `mcp` extra (bundled in the deb). Tool list: [../reference/mcp.md](../reference/mcp.md). |
| `timeline.py` | Pure day-building helpers (timeline segments with personal-time masking), shared by the dashboard, the CLI and the MCP `timeline` tool; moved out of `dashboard/app.py`. |
| `notify.py` | `notify-send` wrapper (`send`) and root-side `send_as(user, ...)` via `runuser` and the kid's session bus. |
| `heartbeat.py` | Capture freshness check (`ok`/`stale`/`dead`/`never`, plus `idle` when the kid has no session), `heartbeat` Event writer, `latest_heartbeat` (adds the dashboard-only `watcher_down`) and `capture_problems` (counts of `capture_failed` / `capture_tamper`). |
| `health.py` | Metrics behind `familymonctl health`. |
| `capture/backend.py` | `CaptureBackend` protocol and snapshot dataclass. |
| `capture/kwin_wayland.py` | The only backend (`kwin_wayland`): window list via a KWin script plus D-Bus sink (accepts a push only from KWin's own bus connection with the tick's nonce; counts rejects as `sink_rejected`), screenshot via `spectacle`. Skips the screenshot when the window list did not arrive; unloads the script after each tick. |
| `capture/supervisor.py` | The production capture loop (`capture-supervise`, root): follows the kid's session via logind, writes a nonce-bearing KWin script per tick, runs `capture-probe` as the kid, then does game detection, dedup, persistence and `capture_failed` / `capture_tamper` events as root. |
| `capture/loop.py` | The in-session dev loop (`capture-run`, `capture-once`): snapshot, pHash dedup, persist. |
| `capture/persist.py` | Writes `Capture` rows and PNGs (dirs 0750, files 0640, group `familymon`). |
| `capture/session_state.py` | logind lock/active detection; yields `session_locked` / `session_inactive` suppress reasons. |
| `capture/messaging_skip.py` | Messaging-window matcher (`messaging_apps.yaml`). |
| `capture/redact.py` | Black-box redaction of messaging windows from KWin geometry. |
| `capture/games.py` | Trust-resistant game detection (installed Steam library + curated list) plus the remembered `known_games.json`. |
| `analyze/categories.py` | The 11 `Category` values. |
| `analyze/heuristics.py` | Multi-window classifier with a single-category priority rule. |
| `analyze/classifier_runner.py` | Classifies unclassified captures; `reclassify_heuristic` for `--redo`. |
| `analyze/dedup.py` | pHash ring buffer. |
| `analyze/schema.py` | Classification record and `source` literals. |
| `categorize/apps.py`, `freedesktop.py` | App lookup from root-owned system `.desktop` files only. |
| `categorize/brands.py`, `domains.py` | Title-brand and domain lookups from YAML. |
| `storage/models.py` | SQLAlchemy tables `capture`, `classification`, `event`. |
| `storage/session.py` | `make_engine` (integrity check, `create_all`, additive migration, permissions via `perms.py`) and `make_readonly_engine` (used by the dashboard, `events` and chargeback). |
| `storage/perms.py` | `secure` / `secure_tree`: root-owned state, group-readable by `familymon` (0640/0750) for the DB and the schedule; 0600/0700 otherwise and in dev runs. |
| `storage/migrate.py` | `migrate_user_state`: moves an older release's per-user state into the root state dir (behind `migrate-state`). |
| `storage/retention.py` | `prune_screenshots` / `screenshot_days`: daily deletion of screenshot files older than `retention.screenshots_days`, clearing the rows' paths (behind `retention`). |
| `storage/recovery.py` | Damaged-DB recovery (`ensure_intact`). |
| `policy/schedule.py` | `Schedule`, `ScheduleBlock`, `SchoolBreak`, extras file. |
| `policy/importers.py` | Kings InterHigh timetable JSON importer (`--format kingsinterhigh`). |
| `policy/conflicts.py` | Lesson-time distraction episodes. |
| `policy/livewarn.py` | Live in-lesson warnings (state in `warned.json`). |
| `policy/enforcement.py` | `policy.yaml` to timekpr plan/diff and the apply safety guard. |
| `policy/timekpr_admin.py` | timekpr transport: reads over D-Bus, writes via `timekpra`. |
| `policy/grants.py` | Date-bound one-off exceptions (`grants.json`). |
| `policy/chargeback.py` | Browser-game minutes charged to PlayTime. |
| `policy/personal_time.py` | The parent's evening window: parses `personal_time`, computes today's window from `allowed_hours` and the active grants (`current_window`). The supervisor flags captures taken inside it; the dashboard masks them. |
| `policy/webblock.py` | Chrome managed policy, forbidden-process sweep. |
| `policy/crx.py` | Chrome extension CRX3 build, signing, update manifest. |
| `dashboard/app.py`, `templates/timeline.html` | Read-only FastAPI timeline behind HTTP Basic auth. |
| `chrome_extension/` | MV3 extension (`background.js`, `rules.js`, `blocked.html/js`, `schema.json`, `manifest.json`). |
| `defaults/*.yaml` | Bundled defaults (messaging apps, games, brands, domains, extras, example policy). |

Not implemented: an X11 backend (a docstring in `backend.py` mentions one; no class exists), the OCR/VLM classifiers (`ocr_kw`, `local_vlm`, `claude_code_bootstrap` are reserved source names; still planned, and their dependencies are not declared until the code lands), and any purge of `capture`, `classification` or `event` rows (screenshot files are pruned by `storage/retention.py`).

## Data flow

```
root (system units, everything)
  capture-supervise (60 s) : logind session? -> KWin script (nonce) in /run/familymon/kwin
        -> setpriv to kid: capture-probe -> windows (D-Bus sink) + spectacle PNG + redaction
        -> redacted CaptureResult JSON on stdout
        -> root: game detection, pHash dedup -> captures.db + screenshots/<day>/<id>.png
        failures -> Event capture_failed / capture_tamper
  classify (5 min) -> classification rows
  heartbeat (1 min) -> event rows (status + logind session)
  notify-tick (1 min) -> notify.send_as -> kid's session; notified.json / warned.json
  enforce apply (hourly) : policy.yaml + grants.json -> timekpra
  enforce chargeback (5 min) : captures.db (read-only) -> timekpra --setplaytimeleft
  enforce web-block (1 min) : schedule.yaml -> Chrome policy file -> extension; process sweep

familymon user (dashboard unit, sandboxed)
  dashboard -> reads captures.db (read-only) + schedule.yaml, group access only (metadata only)
```

Capture to dashboard:

1. `capture-supervise` ticks every `FAMILYMON_INTERVAL` seconds (default 60). Per tick it asks logind whether the kid has a graphical session (none: the tick is skipped and nothing is recorded), then runs the probe as the kid. The probe enumerates windows, drops windows that are both skipPager and skipTaskbar, takes a screenshot with `spectacle`, redacts messaging windows, and returns the result. Root then computes pHash, drops duplicates of the last 8 hashes (Hamming distance under 4), checks for a running game, and persists.
2. A `Capture` row is written whenever the window list arrived. If it did not, no screenshot is taken (`window_list_unavailable`) and a `capture_failed` event is written instead. The PNG is written only if bytes exist and the frame is not a duplicate. The DB stores the path, never image bytes.
3. If the session is locked or inactive, window metadata is recorded and the screenshot is skipped (`screenshot_skipped_reason`: `session_locked` or `session_inactive`).
4. `classify` writes one `Classification` per capture. Exactly one category is chosen across all non-minimized visible windows. Priority, highest first: games, video, social, school, homework, code, productivity, messaging, browsing_other, idle, unknown. Ties break on confidence. `present_categories` records the others. Overlap-aware time stats are not built.
5. The dashboard and `conflicts` read captures and classifications. Both use `find_conflicts` (a school-category block plus games/social/messaging, or video with a streaming brand in the title).

Capture to enforcement:

- The reconcilers do not consume classifications directly, except `chargeback`. It reads the capture DB (read-only) and joins `capture` to `classification`, so it only sees captures that `classify` has already labelled `games`, where `game_running` is false and the active window is a browser. It runs only if `policy.yaml` has `games.enabled` and `games.charge_browser_games` set, and it records an audit `Event`. If its stored watermark is above the DB's newest capture id (DB replaced, recovered or migrated), it resets for today instead of never charging again.
- `enforce apply` converts `policy.yaml` (plus active grants) into a minimal ordered list of `timekpra` calls. Allowed-hours changes go last.
- `enforce web-block` (needs `web_block.enabled: true` in `policy.yaml`) takes the schedule's current `school` block (`/var/lib/familymon/schedule.yaml`) and writes `/etc/opt/chrome/policies/managed/familymon.json`. During a lesson the document carries `URLBlocklist`; outside lessons the file is kept with the always-on base keys and the extension entry but no `URLBlocklist`. The force-installed extension reads that policy via managed storage and redirects matching tabs. During a lesson the same tick also closes forbidden processes of the kid, writes `web_block` and `lesson_close` events, and notifies the kid.

## Process and privilege boundaries

| Runs as | Components |
|---|---|
| Root (systemd system units) | `capture-supervise`, `classify`, `heartbeat-once`, `schedule notify-tick`, `enforce apply`, `enforce chargeback`, `enforce web-block`, plus every manual operator command (`schedule import/refresh`, `enforce grant/revoke`, `health`, `events`). State is `/var/lib/familymon` (root:`familymon` 0750). |
| The kid, one tick at a time | `capture-probe`, started by the supervisor through `setpriv --reuid=<uid> --regid=<gid> --init-groups --no-new-privs -- env -i ... python -I -m familymon.cli capture-probe`. It is the only familymon code that runs in the kid's session. It writes no state and is non-dumpable. |
| Root, per SSH session | `familymonctl mcp`: the MCP server, started by the parent's agent as `ssh kid-laptop sudo -n familymonctl mcp` (allowed without a password for the `familymon-admin` group by `/etc/sudoers.d/familymon-mcp`). Speaks the protocol on stdin/stdout, listens on nothing, lives only as long as the SSH session, and runs the same `ops.py` functions as the operator commands. |
| `familymon` system user | `dashboard`, read-only: group access to the DB and the schedule, nothing else, with systemd sandboxing. |
| Inside Chrome | The extension; no root. |

Boundary rules:

- `state_dir()` is `FAMILYMON_STATE_DIR` if set (only the dashboard unit sets it); otherwise root uses `/var/lib/familymon` and others use `$XDG_STATE_HOME/familymon` (default `~/.local/state/familymon`, dev only). Nothing reads the kid's home except `migrate-state` (`user_state_dir(user)`).
- Why a probe at all: KWin authorizes window enumeration and screenshots by being a client of the user's own Plasma session; root cannot be one (tested: `spectacle` as root reports that KWin "does not seem to be available"). So root supervises and a short-lived helper observes. The helper's request (script path, nonce, suppress reason) arrives on stdin and its redacted `CaptureResult` JSON leaves on stdout; root re-stamps the capture time and does everything else.
- The KWin script for each tick lives in `/run/familymon/kwin` (0711 dir, root-owned 0644 file with a random name, deleted after the tick), so the kid can neither list nor change it. The sink rejects pushes whose D-Bus sender is not KWin or whose nonce is wrong.
- The capture unit has `PrivateTmp=yes`, so the probe's temporary screenshot file is in a `/tmp` the kid cannot see.
- Root notifies the kid with `notify.send_as`: `runuser -u <user> -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus ... notify-send`. It returns whether the notice was delivered; `notify-tick` marks a block start or warning done only on delivery.
- timekpr reads go over the system D-Bus (`com.timekpr.server`); writes go through the `timekpra` CLI. Requires root or `timekpr` group.
- Safety: the process sweep refuses uid 0 and requires euid 0 to kill, so a dev run never kills its own browser.
- Residual, detected not prevented: the kid can make ticks fail (log out, restart KWin, kill the probe, squat `org.familymon.WindowSink`). That produces `capture_failed` / `capture_tamper` events and a dead heartbeat while the session is open. The full threat model is in [tamper-resistance.md](tamper-resistance.md).

## Design invariants

Preserve these when changing code. Parent decides policy; do not change them to fit one data point.

- **Privacy.** Messaging windows are blacked out by KWin geometry (pixels are never analyzed). If redaction cannot be done safely (scale mismatch over 1%, bad geometry, unreadable image), the screenshot is dropped (`messaging_app`) rather than stored. `messaging_present` is recorded per capture so `health` can detect `privacy_violations` (exit 2). The state directory is root:`familymon` 0750; the DB and screenshots are group-readable by `familymon` only (0640/0750) and the kid has no access. The dashboard serves metadata only, never images, and opens the DB read-only. A missing KWin window list means no screenshot at all, because redaction needs the geometry.
- **Trust-resistant signals.** The monitored user must never control a signal that pauses or disables monitoring. GameMode was rejected for this reason. Game detection uses the installed Steam library (`appmanifest_*.acf`, `steamapps/common/<installdir>`) and a curated list. App categories come only from root-owned system `.desktop` directories, never `~/.local/share/applications`.
- **Fail-open capture.** logind errors never stop capture. Tick exceptions are logged and the loop continues. No graphical session skips the tick (nothing to capture); a probe failure while a session exists is recorded as `capture_failed`, never swallowed.
- **Read-only where only reading is needed.** `chargeback`, `events` and the dashboard open the DB with `mode=ro` (`make_readonly_engine`), so they cannot create files or run recovery.
- **Never trust the probe's output blindly.** Root parses and validates the probe's JSON, stamps its own capture time, and does game detection and persistence itself. New fields the probe returns must not be able to widen what root writes.
- **Idempotent reconcilers.** `enforce apply` writes only deltas. `web-block` is stateless: it rewrites the Chrome policy only if the JSON differs, and deletes the file only when `web_block` is disabled. A missed tick self-corrects. `schedule refresh` and `apply_extras` re-apply as no-ops. Chargeback advances its watermark only after timekpr accepts the change.
- **Never disrupt silently.** The apply guard refuses (exit 3) a change that would cut an active session unless `--force`. One-off extensions use grants, which only add hours; a grant runs the same guard, because applying it would also apply any pending policy change (when the whole plan would cut the session, only today's widened window is applied). Never use raw `timekpra --setallowedhours`.
- **Self-healing DB.** `make_engine` runs an integrity check first; on damage it moves the file aside (`captures.db.corrupt-<timestamp>`, never deleted), recovers with `sqlite3 .recover`, then `iterdump`, then a fresh DB, and records a `db_recovered` event. Additive column migration is idempotent.
- **Units.** Every production unit is a system unit. Do not add user units to the deb: the kid owns their user manager and could stop or mask them.
- **Separate bug from policy.** Establish facts from the data, fix bugs, and present policy trade-offs to the parent with examples.

## Caveats

- `familymonctl health` and `conflicts` open the DB through `make_engine`, so they can run recovery or migration. They are not strictly side-effect free. The dashboard and `events` use the read-only engine.
- `enforce plan` can prune expired grants from `grants.json`.
- The deb's postinst creates the `familymon` system user and `/var/lib/familymon`; `capture-run` (dev) and `capture-once` still use the in-session `loop.py`, which does not go through the supervisor.
