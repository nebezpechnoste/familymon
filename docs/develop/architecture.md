# Architecture

Read this when you need to change familymon's code, or need to know which component runs as which user, where data flows, and which invariants must not be broken. For commands see [../reference/cli.md](../reference/cli.md). For paths and units see [../reference/files-and-units.md](../reference/files-and-units.md). For terms see [../concepts.md](../concepts.md). For tests and releases see [testing-and-shipping.md](testing-and-shipping.md).

familymon is local-first. All data stays on the monitored laptop (`kid-laptop`). Monitoring is disclosed. The parent decides policy.

## Package map

CLI entry point: `familymonctl = "familymon.cli:main"` (`pyproject.toml`). Everything is under `familymon/`.

| Module | Role |
|---|---|
| `cli.py` | All `familymonctl` commands (capture, classify, health, schedule, conflicts, enforce, extension, dashboard). |
| `config.py` | `state_dir()`, `user_state_dir(user)`, DB/screenshot/schedule/policy paths, `config_file(name)` override order, `capture_interval_seconds()`. |
| `notify.py` | `notify-send` wrapper (`send`) and root-side `send_as(user, ...)` via `runuser` and the kid's session bus. |
| `heartbeat.py` | Capture freshness check (`ok`/`stale`/`dead`/`never`) and `heartbeat` Event writer. |
| `health.py` | Metrics behind `familymonctl health`. |
| `capture/backend.py` | `CaptureBackend` protocol and snapshot dataclass. |
| `capture/kwin_wayland.py` | The only backend (`kwin_wayland`): window list via a KWin script plus D-Bus sink, screenshot via `spectacle`. |
| `capture/loop.py` | Per-tick order: snapshot, pHash dedup, persist. |
| `capture/persist.py` | Writes `Capture` rows and PNGs (dirs 0700). |
| `capture/session_state.py` | logind lock/active detection; yields `session_locked` / `session_inactive` suppress reasons. |
| `capture/messaging_skip.py` | Messaging-window matcher (`messaging_apps.yaml`). |
| `capture/redact.py` | Black-box redaction of messaging windows from KWin geometry. |
| `capture/games.py` | Trust-resistant game detection (installed Steam library + curated list). |
| `analyze/categories.py` | The 11 `Category` values. |
| `analyze/heuristics.py` | Multi-window classifier with a single-category priority rule. |
| `analyze/classifier_runner.py` | Classifies unclassified captures; `reclassify_heuristic` for `--redo`. |
| `analyze/dedup.py` | pHash ring buffer. |
| `analyze/schema.py` | Classification record and `source` literals. |
| `categorize/apps.py`, `freedesktop.py` | App lookup from root-owned system `.desktop` files only. |
| `categorize/brands.py`, `domains.py` | Title-brand and domain lookups from YAML. |
| `storage/models.py` | SQLAlchemy tables `capture`, `classification`, `event`. |
| `storage/session.py` | `make_engine`: integrity check, `create_all`, additive migration, 0600/0700 permissions. |
| `storage/recovery.py` | Damaged-DB recovery (`ensure_intact`). |
| `policy/schedule.py` | `Schedule`, `ScheduleBlock`, `SchoolBreak`, extras file. |
| `policy/importers.py` | Kings InterHigh timetable JSON importer (`--format kingsinterhigh`). |
| `policy/conflicts.py` | Lesson-time distraction episodes. |
| `policy/livewarn.py` | Live in-lesson warnings (state in `warned.json`). |
| `policy/enforcement.py` | `policy.yaml` to timekpr plan/diff and the apply safety guard. |
| `policy/timekpr_admin.py` | timekpr transport: reads over D-Bus, writes via `timekpra`. |
| `policy/grants.py` | Date-bound one-off exceptions (`grants.json`). |
| `policy/chargeback.py` | Browser-game minutes charged to PlayTime. |
| `policy/webblock.py` | Chrome managed policy, forbidden-process sweep. |
| `policy/crx.py` | Chrome extension CRX3 build, signing, update manifest. |
| `dashboard/app.py`, `templates/timeline.html` | Read-only FastAPI timeline behind HTTP Basic auth. |
| `chrome_extension/` | MV3 extension (`background.js`, `rules.js`, `blocked.html/js`, `schema.json`, `manifest.json`). |
| `defaults/*.yaml` | Bundled defaults (messaging apps, games, brands, domains, extras, example policy). |

Not implemented: an X11 backend (a docstring in `backend.py` mentions one; no class exists), OCR/VLM classifiers (`ocr_kw`, `local_vlm`, `claude_code_bootstrap` are reserved source names), and any retention or purge of captures, events or screenshots.

## Data flow

```
kid session (user units)
  capture-run -> KWin script -> windows (D-Bus sink)
              -> spectacle -> PNG -> redact (messaging) -> pHash dedup
              -> captures.db (capture rows) + screenshots/<day>/<id>.png
  classify (5 min) -> classification rows
  heartbeat (1 min) -> event rows
  notify-tick (1 min) -> notify-send; notified.json / warned.json
  dashboard -> reads captures.db + schedule.yaml (metadata only)

root (system units)
  enforce apply (hourly) : policy.yaml + grants.json -> timekpra
  enforce chargeback (5 min) : kid's captures.db (read-only) -> timekpra --setplaytimeleft
  enforce web-block (1 min) : schedule.yaml -> Chrome policy file -> extension; process sweep
```

Capture to dashboard:

1. `capture-run` ticks every `FAMILYMON_INTERVAL` seconds (default 60). Per tick: enumerate windows, drop windows that are both skipPager and skipTaskbar, take a screenshot with `spectacle`, redact messaging windows, compute pHash, drop duplicates of the last 8 hashes (Hamming distance under 4), persist.
2. A `Capture` row is always written. The PNG is written only if bytes exist and the frame is not a duplicate. The DB stores the path, never image bytes.
3. If the session is locked or inactive, window metadata is recorded and the screenshot is skipped (`screenshot_skipped_reason`: `session_locked` or `session_inactive`).
4. `classify` writes one `Classification` per capture. Exactly one category is chosen across all non-minimized visible windows. Priority, highest first: games, video, social, school, homework, code, productivity, messaging, browsing_other, idle, unknown. Ties break on confidence. `present_categories` records the others. Overlap-aware time stats are not built.
5. The dashboard and `conflicts` read captures and classifications. Both use `find_conflicts` (a school-category block plus games/social/messaging, or video with a streaming brand in the title).

Capture to enforcement:

- The root reconcilers do not consume classifications directly, except `chargeback`. It reads the kid's DB (read-only) and joins `capture` to `classification`, so it only sees captures that `classify` has already labelled `games`, where `game_running` is false and the active window is a browser. It runs only if `policy.yaml` has `games.enabled` and `games.charge_browser_games` set, and it records an audit `Event` in the root DB.
- `enforce apply` converts `policy.yaml` (plus active grants) into a minimal ordered list of `timekpra` calls. Allowed-hours changes go last.
- `enforce web-block` (needs `web_block.enabled: true` in `policy.yaml`) takes the kid's schedule's current `school` block and writes `/etc/opt/chrome/policies/managed/familymon.json`. During a lesson the document carries `URLBlocklist`; outside lessons the file is kept with the always-on base keys and the extension entry but no `URLBlocklist`. The force-installed extension reads that policy via managed storage and redirects matching tabs. During a lesson the same tick also closes forbidden processes of the kid, writes `web_block` and `lesson_close` events, and notifies the kid.

## Process and privilege boundaries

| Runs as | Components |
|---|---|
| Kid's user session (systemd user units, no root) | `capture-run`, `classify`, `heartbeat-once`, `schedule notify-tick`, `dashboard`, `schedule import/refresh`. Capture must run inside the monitored user's graphical session. |
| Root (systemd system units) | `enforce apply`, `enforce chargeback`, `enforce web-block`; manual `enforce grant/revoke`. Root state is `/var/lib/familymon`. |
| Inside Chrome | The extension; no root. |

Boundary rules:

- `state_dir()` is `FAMILYMON_STATE_DIR` if set; otherwise root uses `/var/lib/familymon` and others use `$XDG_STATE_HOME/familymon` (default `~/.local/state/familymon`). Root jobs read the kid's data via `user_state_dir(user)`, which ignores `FAMILYMON_STATE_DIR` and `XDG_STATE_HOME`.
- Root notifies the kid with `runuser -u <user> -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus ... notify-send`. A missing bus is silent.
- timekpr reads go over the system D-Bus (`com.timekpr.server`); writes go through the `timekpra` CLI. Requires root or `timekpr` group.
- Safety: the process sweep refuses uid 0 and requires euid 0 to kill, so a dev run never kills its own browser.
- Known limitation: any same-UID process on the session bus could forge the window-list `Push`. Another: the heartbeat watcher is a user unit; moving it to root is noted as future work in code.

## Design invariants

Preserve these when changing code. Parent decides policy; do not change them to fit one data point.

- **Privacy.** Messaging windows are blacked out by KWin geometry (pixels are never analyzed). If redaction cannot be done safely (scale mismatch over 1%, bad geometry, unreadable image), the screenshot is dropped (`messaging_app`) rather than stored. `messaging_present` is recorded per capture so `health` can detect `privacy_violations` (exit 2). The DB is 0600 in a 0700 directory; screenshot dirs are 0700. The dashboard serves metadata only, never images.
- **Trust-resistant signals.** The monitored user must never control a signal that pauses or disables monitoring. GameMode was rejected for this reason. Game detection uses the installed Steam library (`appmanifest_*.acf`, `steamapps/common/<installdir>`) and a curated list. App categories come only from root-owned system `.desktop` directories, never `~/.local/share/applications`.
- **Fail-open capture.** logind errors or no graphical session never stop capture. Tick exceptions are logged and the loop continues.
- **Read-only access to the kid's DB from root.** The root `chargeback` job opens it with SQLite URI `mode=ro` so no root-owned journals appear in the kid's directory.
- **Idempotent reconcilers.** `enforce apply` writes only deltas. `web-block` is stateless: it rewrites the Chrome policy only if the JSON differs, and deletes the file only when `web_block` is disabled. A missed tick self-corrects. `schedule refresh` and `apply_extras` re-apply as no-ops. Chargeback advances its watermark only after timekpr accepts the change.
- **Never disrupt silently.** The apply guard refuses (exit 3) a change that would cut an active session unless `--force`. One-off extensions use grants, which only add hours. Never use raw `timekpra --setallowedhours`.
- **Self-healing DB.** `make_engine` runs an integrity check first; on damage it moves the file aside (`captures.db.corrupt-<timestamp>`, never deleted), recovers with `sqlite3 .recover`, then `iterdump`, then a fresh DB, and records a `db_recovered` event. Additive column migration is idempotent.
- **Timers.** User timers use `OnActiveSec`, not `OnBootSec`, so they arm when the user logs in long after boot.
- **Separate bug from policy.** Establish facts from the data, fix bugs, and present policy trade-offs to the parent with examples.

## Caveats

- `familymonctl health`, `conflicts` and `dashboard` open the DB through `make_engine`, so they can run recovery or migration. They are not strictly side-effect free.
- `enforce plan` can prune expired grants from `grants.json`.
- Unsure or not verified: whether the deb creates a `familymon` system user or `/var/lib/familymon`. No packaging script does; a `config.py` docstring mentions a `familymon` system user, but nothing in the repo creates one.
