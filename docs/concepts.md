# Concepts

Read this when you are new to familymon and need the mental model before touching anything: what the system watches, how a screenshot becomes a category, how a category becomes a conflict or a block, which component enforces what, and what the system does and does not defend against. Commands, keys and unit names are only summarised here; the exact forms live in [reference/cli.md](reference/cli.md), [reference/config.md](reference/config.md) and [reference/files-and-units.md](reference/files-and-units.md).

Placeholders used across these docs: the monitored account is `kid`, the laptop is `kid-laptop`, the timezone is Europe/Berlin, example addresses come from 192.0.2.0/24.

## 1. The idea: monitor and react

Familymon is local-first monitoring plus enforcement for a Linux/KDE (Wayland) laptop. It has two halves.

- **Monitor (reactive, logged).** A root supervisor follows the kid's graphical session, records which windows are open, takes a screenshot with messaging windows blacked out, and classifies each sample into one category. Nothing leaves the machine. The parent reads the result in a local dashboard or CLI.
- **Enforce (preventive).** Time and per-app limits are delegated to timekpr-next. Web blocking during lessons is delegated to Chrome managed policy, a force-installed extension and a process sweep. Familymon only computes the desired state and pushes it; it does not build its own session or time enforcement.

The two halves connect through the schedule: the schedule says when a lesson is in progress, and the lesson is what turns "the kid has a game open" from a log line into a conflict, a warning or a block.

Monitoring is disclosed to the kid. It is not covert.

## 2. Actors

| Actor | Role | Authority |
|---|---|---|
| Parent | Owns policy. Chooses categories, limits, schedule, which domains are blocked. | Decides every policy question. |
| Kid (`kid`) | The monitored user. Uses `kid-laptop` normally. | Controls nothing that disables monitoring (see section 8). |
| Agent | An AI coding/ops agent (for example Claude Code) acting for the parent: installs, configures from intent, verifies health, troubleshoots. | Executes the parent's decisions. Establishes facts, fixes bugs, presents policy trade-offs. Never invents policy. |

### Agent working rules

1. **Parent decides.** Establish facts from the data first. Separate a bug (code did not do what the rule says) from a policy question (the rule itself). Fix bugs. Present policy trade-offs to the parent with examples. Never change classification or enforcement policy on inference from one ambiguous data point.
2. **Safety: never take a raw screenshot of the kid's session for diagnostics.** It bypasses the messaging-window redaction. Verify notifications through the desktop's notification history or D-Bus state, or through familymon's own (redacting) capture.
3. **Tests and dev runs never touch real processes or real user state.** Destructive paths are tested only against things the test spawned. The process sweep refuses to run against uid 0 and refuses to kill without root, to protect a dev machine's own browser.
4. **Gate on real exit codes.** Do not chain a test run through a pipe into a build or publish step (`pytest | tail && build` hides the exit code). Verify and ship in separate steps.
5. **Read the capture DB read-only from jobs that only report on it** (SQLite URI `mode=ro`): chargeback, `events`, the dashboard. The capture supervisor, classifier and heartbeat are the only writers.
6. **Every operator command runs as root** (`sudo familymonctl ...`). The kid has no access to familymon's state, so there is no "run it as the kid" any more.

See [develop/testing-and-shipping.md](develop/testing-and-shipping.md) and [operate/verify-and-health.md](operate/verify-and-health.md).

## 3. The pipeline

```
capture ──► classify ──► schedule ──► conflicts ──► warnings / enforcement
 (root)      (root)       (data)       (report)       (root)
```

| Stage | What it does | Runs as | Cadence |
|---|---|---|---|
| Capture | Root supervisor follows the kid's session and, per tick, runs a short-lived `capture-probe` as the kid that enumerates windows via KWin, screenshots via `spectacle` and redacts messaging windows. Root then does game detection, pHash dedup and writes a `Capture` row | root, system unit `familymon-capture.service` running `capture-supervise` | `FAMILYMON_INTERVAL` seconds, default 60 |
| Classify | Give each unclassified capture exactly one category (heuristics only) | root, `familymon-classify.timer` running `classify` | 2 min after boot, then every 5 min |
| Schedule | Weekly blocks (lessons and extras) plus date-range breaks, stored in `schedule.yaml` | data file, imported or hand-written | re-read by every consumer on every run |
| Conflicts | A `school` block overlapping a distraction capture becomes a conflict episode | CLI `conflicts` and the dashboard, same function | on demand |
| Live warnings | Notification to the kid during a lesson when a distraction is seen. Runs as root and delivers into the kid's session; a notice counts as done only if it was delivered | root, `familymon-notify.timer` running `schedule notify-tick` | every 1 min |
| Enforcement | timekpr limits, Chrome policy, extension, process sweep, play-time chargeback | root, system units `familymon-enforce`, `familymon-webblock`, `familymon-chargeback` | see section 6 |

A heartbeat unit (`familymon-heartbeat.timer`, root, every minute) records whether capture is fresh and whether the kid has a graphical session. `familymonctl health` reports on the whole pipeline. See [operate/verify-and-health.md](operate/verify-and-health.md).

### 3.1 Capture details that matter conceptually

- If the kid has no graphical session (logged out, text console), the tick is skipped and nothing is recorded; the heartbeat reads that as `idle`, not as a fault.
- Each tick takes window metadata first, then the screenshot. A `Capture` row is written whenever the window list arrived. The PNG is kept only if it is visually novel: a perceptual-hash ring buffer (8 hashes, Hamming threshold 4) marks near-duplicates `same_as_prev` and skips saving the file. Dedup keys on visual novelty, not on "same active window".
- The only capture backend is `kwin_wayland`. Per tick the supervisor writes a KWin script that carries a random nonce, the probe loads it into KWin, and KWin pushes window data back over the session bus. The sink accepts the push only from KWin's own bus connection with the right nonce; anything else is rejected and logged as `capture_tamper`. The script is unloaded after the tick. Screenshots use `spectacle` because KWin's own screenshot D-Bus interface rejects unauthorized callers, and because `spectacle` run by root itself cannot reach the kid's KWin; that is why the probe runs as the kid.
- If the window list does not arrive, no screenshot is taken (skip reason `window_list_unavailable`), because redaction needs the window geometry. If the probe fails while a session exists, the tick records a `capture_failed` event instead of a capture.
- If the session is locked or inactive (checked through logind, on the seated graphical session only), window metadata is still recorded and the screenshot is skipped with reason `session_locked` or `session_inactive`. Any logind error fails open: capture continues.
- Messaging windows are redacted before anything is written (section 8).
- The game library is scanned at supervisor start and re-scanned every 30 minutes, so a game installed mid-session counts without a restart. Games seen installed are remembered in `known_games.json`, so hiding a game's files does not remove its PlayTime mask (`familymonctl enforce games`).
- Capture does not read the schedule.

### 3.2 DB self-healing

Every read-write DB user opens the DB through `make_engine`, which first runs an integrity check (chargeback and the dashboard open it through a separate read-only engine instead). If the file is damaged it is moved aside as `<db>.corrupt-<timestamp>` (never deleted), then recovery is attempted in order: `sqlite3 .recover`, Python `iterdump`, then an empty DB. Monitoring continues in every case and a `db_recovered` event is recorded. `familymonctl health` surfaces it. Because opening can write, "read-only" commands that open the DB are not strictly side-effect free.

### 3.3 Unclassified rows

Captures that the classifier has not reached have no classification row; the dashboard shows them as `unknown`, and `familymonctl conflicts` and chargeback ignore them (both join on classification rows). If many rows are `unknown` after an outage, run `familymonctl classify` and, to relabel history, `classify --redo` (it deletes heuristic-sourced classification rows and recomputes them; see [reference/cli.md](reference/cli.md)). The classify timer is a system timer, so it runs whether or not the kid is logged in.

## 4. Categories and the multi-window priority rule

### 4.1 Categories

Eleven values: `school`, `homework`, `games`, `social`, `messaging`, `video`, `code`, `productivity`, `browsing_other`, `idle`, `unknown`.

- `idle` is assigned when the session is locked or inactive (confidence 0.9).
- `unknown` means no visible window gave any signal (confidence 0.1), or the classifier raised an error on that row (confidence 0.0).
- `homework` is a valid category and has a priority slot, but no default app mapping, brand or domain assigns it.

### 4.2 One category per capture

Exactly one category is chosen per capture across all non-minimized visible windows, not only the focused one. Each window yields at most one verdict; the winner is the verdict with the lowest priority index, ties broken by higher confidence.

Priority, highest first:

1. `games`
2. `video`
3. `social`
4. `school`
5. `homework`
6. `code`
7. `productivity`
8. `messaging`
9. `browsing_other`
10. `idle`
11. `unknown`

Consequences:

- A visible game beats everything. A game running anywhere (installed-game process) adds a `games` verdict at confidence 0.95.
- A lesson window plus a YouTube tab classifies as `video`, not `school`.
- The record also stores `present_categories` (all distinct verdict categories in priority order, winner first). The dashboard shows non-winning ones as "also:". Overlap-aware time statistics are not built yet: time-by-category uses only the primary category.

Parent decides: the priority order is policy. The agreed future direction is a hybrid multi-label model. Do not reorder categories on one surprising row; establish whether it was a bug or the rule working as designed.

### 4.3 Per-window signals

| Window type | Signal order | Confidence |
|---|---|---|
| Browser | 1. a domain-looking token in the title found in the domain table | 0.7, source `domain` |
|  | 2. a brand substring in the title | 0.6, source `heuristic` |
|  | 3. otherwise `browsing_other` | 0.5, source `heuristic` |
| Non-browser | window class looked up in installed `.desktop` entries | 0.85, source `heuristic` |
| Any, with `game_running` | installed game process | 0.95, source `game_process` |

Rules of thumb:

- App lookup scans only root-owned system `.desktop` directories (plus any `XDG_DATA_DIRS` entries that are not under the kid's home). The per-user `~/.local/share/applications` is excluded because `Categories=` there is forgeable by the kid. User-space-only apps therefore classify as `unknown`; games are detected separately.
- Brand matching is a case-insensitive substring match, first match in file order wins. The `games` brands are deliberately last so a YouTube or Wikipedia page about games keeps its video or school label.
- The schedule's lesson group codes are appended as `school` brands at classify time. Subject names are not used.
- Domain matching is apex-suffix: `m.youtube.com` matches `youtube.com`.
- Only heuristics are built. `ocr_kw`, `local_vlm` and `claude_code_bootstrap` exist as reserved source names and are not implemented.
- The defaults live in `messaging_apps.yaml`, `game_apps.yaml`, `domains_starter.yaml`, `title_brands.yaml`. Resolution order for an override is `$FAMILYMON_CONFIG_DIR/<name>`, then `/etc/familymon/<name>`, then the bundled copy. See [reference/config.md](reference/config.md).

## 5. Schedule: blocks, categories, breaks

`schedule.yaml` holds a timezone label, weekly `blocks` and date-range `breaks`. All times are the kid's local time (the `timezone` field is stored and displayed only; no logic uses it).

A block has `day`, `start`, `end` (quote the `HH:MM` strings), `subject`, `category` (default `school`), optional `group_name`, and `notify` (default false). A block covers `start <= t < end`.

| Block category | Conflict-monitored | Web-blocked | Notifies at start |
|---|---|---|---|
| `school` | yes | yes | if `notify: true` |
| anything else (self-study, routine) | no | no | if `notify: true` |

A break suspends blocks of its listed categories (default `school`) on a date range, inclusive. The blocks stay in the file. A break turns off conflicts, live warnings, web blocking and start notifications for those blocks, and the dashboard shows a "School break" banner.

Sources of a schedule:

- **Importer.** `familymonctl schedule import FILE --format kingsinterhigh` reads the Kings InterHigh student API response from `GET /student/api/timetable/week` (fetched by the parent once a term). All lessons become `school` blocks. Break periods from the API are not imported. Import builds the schedule from the API response, merges the extras file, and overwrites `schedule.yaml`.
- **Extras.** `/etc/familymon/schedule_extras.yaml` can exclude subjects, add blocks and define breaks. It is applied only by `schedule import` and `schedule refresh`; edits do nothing until one of those runs.
- **By hand.** Nothing requires the importer. A minimal file with `timezone` and `blocks` loads.

Caveat: `schedule refresh` keeps only `school` blocks from the existing file and rebuilds all else from the extras. Every schedule command runs as root and the one schedule file is `/var/lib/familymon/schedule.yaml`, which every consumer (web-block, notify, conflicts, dashboard) reads.

## 6. What is enforced by whom

| Concern | Enforced by | Mechanism | Runs as | Cadence |
|---|---|---|---|---|
| Allowed hours, daily time limit, lockout type | timekpr-next | `enforce apply` converts `policy.yaml` into a minimal diff of `timekpra` writes. Reads go over D-Bus | root | `familymon-enforce.timer` (hourly) or manual |
| Game and app time (PlayTime) | timekpr-next | Process masks from the kid's Steam library plus `games.extra_masks` | root | same as above |
| Browser-game time | familymon chargeback | Deducts play-time-left for today's browser-focused captures classified `games` with no native game running. Needs `games.enabled` and `games.charge_browser_games` in `policy.yaml`; `games.charge_browser_games_when` is `visible` (default) or `focused`. Depends on the classify timer having already labelled the captures: unclassified captures are not charged until a later tick | root | `familymon-chargeback.timer` (every 5 min) |
| New web navigation during a lesson | Chrome managed policy | `URLBlocklist` in `/etc/opt/chrome/policies/managed/familymon.json`, plus DoH and ECH off, DevTools policy, private windows off per `block_private` | root writes the file | `familymon-webblock.timer` (every minute) |
| Tabs already open when a lesson starts | Chrome extension | Force-installed MV3 extension redirects matching tabs to a local "blocked" page | inside Chrome, no root | on policy change, tab update, and a 1-minute alarm |
| Other browsers, Chrome with `--disable-extensions`, `forbid_apps` | Process sweep | SIGTERM, 3 s grace, then SIGKILL, only during a lesson, with a notice to the kid | root | same tick as web-block |
| Warning about distraction the web layer cannot stop (native game, streaming video, chat) | Live warnings | Desktop notification, "This is logged." | root, delivered into the kid's session | `familymon-notify.timer` (every minute) |

Ordering inside the web-block tick: the Chrome policy file is reconciled, then the sweep runs if a lesson is active.

Points to keep straight:

- timekpr is the only time and app enforcer. Familymon computes desired state and diffs it against live state; it never reimplements limits.
- `video` is not in the default web-block categories. Streaming during a lesson is flagged and warned about, not blocked. Parent decides whether to add `video` to `web_block.categories`.
- `meet.google.com`, `teams.microsoft.com` and `zoom.us` are never blocked.
- The extension does not run in private windows. That is why `block_private` exists (`lessons`, `always` or `off`).
- Only Chrome is governed as a browser. Other browsers are closed during lessons when `forbid_other_browsers` is true.
- Native apps are timekpr's domain unless listed in `forbid_apps`.
- Policy changes take effect on the next run of the relevant consumer, not live: `enforce apply` for timekpr keys, the next chargeback tick for `games.charge_browser_games*`, the next minute for `web_block.*`.

### Safety rules for enforcement

- **Safety:** changing allowed hours so the current hour is excluded while the kid is logged in would end the session. `enforce apply` refuses (exit 3) unless `--force`, if a session exists and either today's window excludes now or today's limit would shrink. Window changes are applied last in the plan for that reason.
- One-off extensions use date-bound grants (`enforce grant`), never raw `timekpra --setallowedhours`. Raw changes are persistent and the hourly reconciler reverts them. Grants only widen today's hours and expire on their own.
- The `timekpra --getuserinfo` command prints enabled booleans as `False` (a CLI bug in the version in use), so familymon reads via D-Bus.
- timekpr PlayTime does nothing until the global PlayTime switches in `/etc/timekpr/timekpr.conf` (including the enhanced activity monitor) are enabled. Per-user settings are silently masked until then. Familymon does not edit that file; it is a manual prerequisite.
- PlayTime masks are effectively full-match against process path and command line. The driver wraps bare `games.extra_masks` entries as `.*mask.*`. Flatpak apps appear as their `bwrap` wrapper.
- timekpr-next behaviour observed with 0.5.10 (not familymon code): timekpr applies a play-time-left change on its next poll (about 3 s), so read back after a few seconds; and suspend and lock lockouts are only enforced while the session is active.
- After a package upgrade, long-running units may still run old code. The deb's postinst runs `daemon-reload`, then enables and restarts `familymon-capture.service` and `familymon-dashboard.service` and starts all seven timers; the timer-driven services pick up new code by themselves. If behaviour looks stale, compare the unit's `ActiveEnterTimestamp` with the package install time.
- The package enables everything itself (capture, dashboard, and the classify, heartbeat, notify, enforce, chargeback and webblock timers). Units that need configuration skip themselves until it exists: capture, notify, enforce, chargeback and webblock wait for `/var/lib/familymon/policy.yaml`, and the dashboard waits for `/etc/familymon/dashboard.env`.

Details: [operate/configure-from-intent.md](operate/configure-from-intent.md), [reference/config.md](reference/config.md).

## 7. Conflicts and live warnings

A conflict is a capture that falls inside a `school` block and whose activity is a distraction.

- Default distraction categories: `games`, `social`, `messaging`.
- `video` counts only when the window title contains a known streaming brand (for example youtube, netflix, twitch). A bare `video` category does not.
- `browsing_other` is never a distraction, because the online class itself runs in the browser.
- Contiguous conflicting captures with the same subject and category merge into one episode. Duration is estimated as `span * count / (count - 1)`, or one interval for a single capture.
- The CLI (`familymonctl conflicts`) and the dashboard share the same function.
- A planned inversion to a school-platform allowlist is not implemented.

Live warnings (`schedule notify-tick`) apply the same rules to the lesson in progress now, looking back 3 minutes, with a 5-minute cooldown per lesson and category. The warning judges the active window on its own first, so a game in the background does not trigger a "game" warning while the kid is on the class page. The text says the event is logged. A conflict that ended with the lesson is not warned about.

## 8. Privacy contract and threat model

### 8.1 What familymon records

- Window class, title, pid, executable path, geometry and state for every window, each tick, including whether it is on the virtual desktop and activity being shown. A window on another desktop, like a minimized one, is treated as not on screen: it is neither redacted nor counted by the classifier.
- A screenshot per tick when visually novel, stored as a PNG under `/var/lib/familymon/screenshots` (root-only: directories 0700, files 0600; the dashboard shows whether a screenshot exists, never the image). The DB stores only the file path. The DB is root-owned and group-readable by `familymon`; the state directory itself is root:`familymon` 0750, so the kid cannot open any of it.
- Classification, events (heartbeat, DB recovery, grants, chargebacks, web blocks, lesson closes, `capture_failed`, `capture_tamper`, `screenshots_pruned`).
- During the parent's personal-time window (`personal_time` in `policy.yaml`, typically from an evening hour until the bedtime lock) no screenshot is taken. Window class and title are still recorded, because they drive classification and game-time billing, but the dashboard and API show only the category and duration for that period.
- Everything stays on the laptop. The dashboard binds to `127.0.0.1` by default, requires HTTP Basic credentials from `FAMILYMON_DASH_USER` and `FAMILYMON_DASH_PASS`, and refuses to start without them.

### 8.2 Messaging redaction

- Any visible, non-minimized messaging window (the `window_classes` list or a `title_patterns` match, for example WhatsApp Web in any browser) is filled with black in the screenshot before it is saved. Rectangles come from KWin geometry only; pixels are never analysed.
- Windows overlapping a messaging window are also blacked out (bounding-box over-redaction).
- If redaction cannot be done safely (scale mismatch, unreadable image, zero-size window), the screenshot is dropped entirely with reason `messaging_app`. Window metadata is still recorded, so titles of other windows are still in the DB.
- `messaging_present` is recorded per capture. `familymonctl health` fails with exit 2 if any capture has `messaging_present`, a stored screenshot, and `screenshot_redacted` false (the privacy invariant). This check covers all time, not 24 hours.
- **Gap:** a chat open in a browser tab is redacted only if it matches a title pattern (WhatsApp Web by default). Other web chats are classified as messaging but not redacted. URL-based filtering is planned, not built.
- Email clients (Thunderbird, Betterbird) are on the redaction list by default.

**Safety:** never take a diagnostic screenshot of the kid's session. Use notification history, D-Bus state, or familymon's own capture.

### 8.3 Trust-resistant signals

The kid must never control a signal that pauses or disables monitoring. GameMode was considered as a "game is running" signal and rejected, because `gamemoderun firefox` would have switched capture off. Instead:

- Game detection uses the installed Steam library (only directories with a real `appmanifest_*.acf` and an existing `steamapps/common/<installdir>`, excluding Proton and runtime tools) plus a curated, parent-extendable list of window classes and binaries.
- App categories come only from root-owned `.desktop` directories.
- Session suppression uses logind state of the seated graphical session, not an environment variable the kid could set. It fails open.
- The monitoring loop itself is root-owned. The only code that runs as the kid is the per-tick probe, started from root-owned code with a scrubbed environment (`env -i`, `python -I`); its output is parsed and checked by root.

When adding any new signal, ask: can the kid set it from their own account? If yes, it must not be able to reduce monitoring.

### 8.4 What familymon does not defend against

- **Making ticks fail.** The kid can log out, restart KWin, kill each probe, or squat the `org.familymon.WindowSink` bus name. Capture then fails for that tick. This is detected, not prevented: `capture_failed` and `capture_tamper` events, a stale or dead heartbeat while the session is open, and a red banner on the dashboard. A forged window-list push is rejected (wrong sender or wrong nonce) and recorded.
- **Notifications are advisory.** The kid can turn off familymon's notifications in Plasma settings. Monitoring and enforcement are unaffected.
- **Text consoles.** A login on a text console has no KWin session and is not captured.
- **Root or physical access.** A kid with root or the ability to boot another OS can stop monitoring. This is out of scope. Everything else (the units, the state, the heartbeat watcher) is root-owned, so stopping or masking a unit needs root. See [develop/tamper-resistance.md](develop/tamper-resistance.md) for the full threat model.
- **Other devices and networks.** Phones, other computers, other accounts and non-Chrome network use outside the laptop are out of scope. There is no DNS layer yet.
- **Non-Chrome browsers.** They are closed during lessons, not filtered. Chrome DevTools are available except on policy-installed extensions.
- **Private windows** without `block_private` set to block them: the extension cannot be forced into them.
- **Already-open connections.** The URL blocklist applies on navigation; already-open tabs rely on the extension. A Chrome started with `--disable-extensions` runs without it, so the sweep closes it during a lesson.
- **Content understanding.** Classification is heuristic (titles, window classes, domains). A deliberately misleading window title can mislabel a capture.
- **Disclosure.** The system assumes the kid knows it is running. It is not designed to hide itself.

Other incident-derived behaviours: Plasma may only show popups for apps with a desktop entry, so familymon ships a `NoDisplay` `.desktop` file and sends the desktop-entry hint on every notification.

## 9. State and where things live

| Item | Location | Notes |
|---|---|---|
| State dir | `/var/lib/familymon` (root:`familymon` 0750). `$FAMILYMON_STATE_DIR` overrides it (the dashboard unit sets it); a non-root dev run uses `${XDG_STATE_HOME:-~/.local/state}/familymon` | One state dir for everything. Earlier releases kept the kid's data in their home; `migrate-state` moved it |
| Capture DB | `<state>/captures.db` | Group-readable by `familymon` |
| Screenshots | `<state>/screenshots/<YYYY-MM-DD>/<capture_id>.png` | Root-only (dirs 0700, files 0600). Day directories older than `retention.screenshots_days` (default 90) are deleted daily by `familymon-retention.timer`; the rows stay |
| Schedule | `<state>/schedule.yaml` | Group-readable; read by every consumer |
| Policy | `<state>/policy.yaml` (root's state dir in production) | Not read from `/etc`; a template ships at `/usr/share/familymon/policy_example.yaml`. Unknown top-level or section keys are not checked, so a typo there is silently ignored (an unknown day name under a per-day map is rejected, as are bad `lockout` and `web_block` values) |
| Grants | `<root state>/grants.json` | |
| Chargeback watermark | `<root state>/playtime_charges.json` | |
| Known games | `<root state>/known_games.json` | Game dirs seen installed; `enforce games` |
| Chrome policy | `/etc/opt/chrome/policies/managed/familymon.json` | Written only by `enforce web-block`, deleted when `web_block.enabled` is false |
| Bundled configs | `/etc/familymon/{messaging_apps,game_apps,schedule_extras}.yaml` (dpkg conffiles) | `domains_starter.yaml` and `title_brands.yaml` are overridden by creating the file in `/etc/familymon/` |

Full tables: [reference/files-and-units.md](reference/files-and-units.md), [reference/config.md](reference/config.md).

## 10. Glossary

| Term | Meaning |
|---|---|
| Agent | The AI coding/ops agent acting for the parent. |
| Block (schedule block) | A weekly time slot with subject and category. `school` blocks are monitored and web-blocked. |
| Break | A date range that suspends blocks of listed categories. |
| Capture | One tick of the loop: window metadata plus optional screenshot, one DB row. |
| Capture supervisor | `capture-supervise`, the long-running root process (`familymon-capture.service`). It runs `capture-probe` as the kid once per tick. `capture-run` is the in-session dev loop. |
| Category | One of the eleven labels (section 4). |
| Chargeback | Deducting browser-game minutes from timekpr PlayTime left, because timekpr cannot see browser games. |
| Classification | The stored label, confidence, evidence, source and `present_categories` for a capture. |
| Conflict | A capture in a `school` block whose activity is a distraction. |
| Dedup / `same_as_prev` | Perceptual-hash match to a recent frame, so the PNG was not saved. Not "same window". |
| Desired state | What `policy.yaml` (plus active grants) says timekpr should hold. |
| Extension | The force-installed Chrome MV3 extension that redirects blocked tabs. |
| Extras | `schedule_extras.yaml`: subject exclusions, extra blocks, breaks. |
| Fail-open | On logind or similar errors, capture continues rather than stopping. |
| Grant | A date-bound, self-expiring widening of today's allowed hours (`enforce grant --until`). A play-time grant (`--playtime`) is not stored as a grant: it adjusts timekpr's play-time-left counter once and is logged as an event. |
| Heartbeat | Event row recording capture freshness and the kid's session state: ok under 180 s, stale 180 to 600 s, dead 600 s or more, never if no captures, idle if the kid has no graphical session. |
| Hybrid / multi-label | The planned upgrade to the single-category rule. |
| Lesson | A `school` block in progress now. |
| Lockout type | What timekpr does at limit: `lock`, `suspend`, `suspendwake`, `terminate`, `kill`, `shutdown`. |
| Messaging redaction | Blacking out messaging windows in a screenshot, or dropping the screenshot. |
| PlayTime | timekpr's per-process-mask game time budget. |
| Present categories | All distinct categories among visible windows, winner first. |
| Priority rule | The ordering that picks one category across visible windows. |
| Reconciler | The hourly `enforce apply` run that restores policy. |
| Sweep | Closing forbidden processes during a lesson (also the extension's tab sweep; context tells which). |
| State dir | See section 9. |
| Suppress reason | Why a screenshot was skipped: `session_locked`, `session_inactive`, `messaging_app`, `window_list_unavailable`. |
| timekpr | timekpr-next, the time and app enforcement backend. |
| Trust-resistant signal | A signal the kid cannot control to reduce monitoring. |
| `unknown` | No signal, or not yet classified. |
| Web block | The lesson-time Chrome policy, extension and sweep combination. |

## 11. Where to go next

- Install: [operate/install.md](operate/install.md)
- Turn parent intent into config: [operate/configure-from-intent.md](operate/configure-from-intent.md)
- Check the system is healthy: [operate/verify-and-health.md](operate/verify-and-health.md)
- When something looks wrong: [operate/troubleshoot.md](operate/troubleshoot.md)
- Exact commands, keys, files and units: [reference/cli.md](reference/cli.md), [reference/config.md](reference/config.md), [reference/files-and-units.md](reference/files-and-units.md)
- Code layout and release process: [develop/architecture.md](develop/architecture.md), [develop/testing-and-shipping.md](develop/testing-and-shipping.md)
