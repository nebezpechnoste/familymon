# Configuration reference

Read this when you need the exact file, key, type, default or reload behaviour of any familymon configuration, or when you must decide whether a change needs `familymonctl enforce apply`, a service restart, or nothing. Related: [concepts](../concepts.md), [configure from intent](../operate/configure-from-intent.md), [verify and health](../operate/verify-and-health.md), [troubleshoot](../operate/troubleshoot.md), [CLI reference](cli.md), [files and units](files-and-units.md).

Placeholders used throughout: monitored account `kid`, host `kid-laptop`, timezone Europe/Berlin.

Conventions:

- "Parent decides" marks a policy choice. Establish facts, show the parent the trade-off with examples, then set it. Do not change policy on inference from one ambiguous data point.
- "Safety" marks anything that can disrupt the kid's session or privacy.
- Most files have no unknown-key detection. A typo (for example `web_blocks:`) is silently ignored and the feature stays at its default. Re-read the file against the tables below after every edit.

## 1. Resolution order and file map

### 1.1 Bundled YAML files (`config_file(name)`)

Resolved on every call, no caching. First hit wins:

1. `$FAMILYMON_CONFIG_DIR/<name>`, if the variable is set and the file exists.
2. `/etc/familymon/<name>`, if it exists.
3. The bundled default `familymon/defaults/<name>` inside the package.

Files that use this order:

| File | Consumer |
|---|---|
| `messaging_apps.yaml` | capture (privacy redaction list) |
| `game_apps.yaml` | capture (`game_running` flag). `enforce` loads it too but uses only the Steam scan from `GameLibrary` (section 7) |
| `domains_starter.yaml` | classifier, live warnings, `enforce web-block`, `focused` chargeback |
| `title_brands.yaml` | classifier, live warnings, `focused` chargeback |
| `schedule_extras.yaml` | `schedule import`, `schedule refresh` |

### 1.2 Files that do NOT use this order

| File | Location | Notes |
|---|---|---|
| `policy.yaml` | `state_dir()/policy.yaml` | Not read from `/etc/familymon` or `$FAMILYMON_CONFIG_DIR`. |
| `schedule.yaml` | `state_dir()/schedule.yaml` (`/var/lib/familymon/schedule.yaml` in production) | One file, read by every consumer. `enforce web-block --schedule FILE` can evaluate a different one. |
| `dashboard.env` | `/etc/familymon/dashboard.env` | Read by systemd (`EnvironmentFile=`) as root, not by Python. The dashboard unit is skipped (`ConditionPathExists`) when the file is missing. |

### 1.3 `state_dir()`

1. `$FAMILYMON_STATE_DIR`, if set.
2. `/var/lib/familymon`, if euid is 0.
3. `${XDG_STATE_HOME:-~/.local/state}/familymon`.

Consequences:

- Production root runs read `/var/lib/familymon/policy.yaml`. A non-root dev run reads `~/.local/state/familymon/policy.yaml` (a different, dev-only state).
- Every schedule and enforcement command runs as root and works on `/var/lib/familymon`. The dashboard (user `familymon`) reaches the same directory because its unit sets `FAMILYMON_STATE_DIR=/var/lib/familymon`; it needs only group read access.
- Other state files in the state dir: `captures.db`, `screenshots/`, `grants.json`, `playtime_charges.json`, `notified.json`, `warned.json`, `known_games.json` (game dirs seen installed; managed with `enforce games`). `captures.db`, `screenshots/` and `schedule.yaml` are group-readable by `familymon`; everything else is root-only.
- The monitored account is read by `config.monitored_user()`: `$FAMILYMON_USER` if set, else the `user:` key of `policy.yaml`. The capture supervisor, heartbeat and notifier use it.

### 1.4 Environment variables

| Variable | Default | Effect |
|---|---|---|
| `FAMILYMON_CONFIG_DIR` | unset | First lookup directory for bundled YAMLs (1.1). |
| `FAMILYMON_STATE_DIR` | unset | Overrides `state_dir()`. The dashboard unit sets it to `/var/lib/familymon` because the dashboard runs as the non-root `familymon` user; root jobs use `/var/lib/familymon` by default. |
| `FAMILYMON_USER` | unset | The monitored account. Overrides the `user:` key of `policy.yaml` for `capture-supervise`, `heartbeat-once`, `migrate-state` and `schedule notify-tick`. Normally leave it unset. |
| `XDG_STATE_HOME` | `~/.local/state` | Non-root state dir base. |
| `FAMILYMON_INTERVAL` | `60` (parsed with `float()`) | Capture interval in seconds. Also used by `conflicts` and chargeback arithmetic. The deb's capture unit does not set it, so the default 60 applies. |
| `FAMILYMON_DASH_USER` | none, required | Dashboard basic-auth user. The dashboard raises `RuntimeError` if user or password is empty. |
| `FAMILYMON_DASH_PASS` | none, required | Dashboard password. |
| `FAMILYMON_DASH_BIND` | `127.0.0.1` | Dashboard bind address. `--bind` overrides. |
| `FAMILYMON_DASH_PORT` | `8765` | Dashboard port. `--port` overrides. |
| `FAMILYMON_APT_HOST` | none | Developer-side only (`scripts/publish-deb.sh`). Irrelevant to operators. |

### 1.5 dpkg conffiles

The deb marks exactly these as conffiles. The three YAMLs are installed from `familymon/defaults/`; the sudoers file is the rule behind `familymonctl mcp` ([mcp.md](mcp.md)):

```
/etc/familymon/messaging_apps.yaml
/etc/familymon/game_apps.yaml
/etc/familymon/schedule_extras.yaml
/etc/sudoers.d/familymon-mcp
```

dpkg preserves local edits on upgrade (stated in the README and build script comments).

Not conffiles:

- `/usr/share/familymon/policy_example.yaml` is a template. Copy it to `state_dir()/policy.yaml` yourself.
- `domains_starter.yaml` and `title_brands.yaml` are not installed to `/etc`. To override, write `/etc/familymon/<name>` with `familymonctl config set <name>` (or by hand).
- `dashboard.env` is not shipped. Create it.
- `policy.yaml` and `schedule.yaml` live in the state dir.

## 2. Which change needs what

| Change | How it takes effect |
|---|---|
| `policy.yaml`: `user`, `lockout`, `allowed_hours`, `daily_limits`, `games.enabled`, `games.daily_limits`, `games.extra_masks` | Not live. Needs `familymonctl enforce apply` (root), or the next run of `familymon-enforce.timer` (`OnActiveSec=5min`, `OnUnitActiveSec=1h`, `RandomizedDelaySec=5min`). The package enables the timer; it acts once `policy.yaml` exists. |
| `policy.yaml`: `games.charge_browser_games`, `games.charge_browser_games_when` | Next chargeback tick (`familymon-chargeback.timer`: `OnActiveSec=3min`, `OnUnitActiveSec=5min`). No `enforce apply`. |
| `policy.yaml`: `web_block.*` | Next `familymon-webblock.timer` tick (every minute). No `enforce apply`. |
| `policy.yaml`: `retention.screenshots_days` | Next run of `familymon-retention.timer` (daily, 03:00 plus up to 1 h; catches up after boot). Preview: `sudo familymonctl retention --dry-run`. No `enforce apply`. |
| `policy.yaml`: `personal_time` | Live: the capture supervisor and the notify tick re-read it every minute, with the active grants. Preview: `sudo familymonctl personal-time`. No `enforce apply`. |
| `schedule.yaml` | Live. Every consumer re-reads it each run (notify-tick every minute, web-block every minute, `classify` every 5 minutes, dashboard per request). The capture loop does not read it. |
| `schedule_extras.yaml` | Not live. Run `sudo familymonctl schedule refresh` (or `schedule import`). |
| `messaging_apps.yaml` | Next tick: every capture probe is a new process and loads the list when it starts. |
| `game_apps.yaml` | Capture: picked up at the supervisor's next game-library scan (at start and every 30 minutes); `sudo systemctl restart familymon-capture.service` applies it at once. `enforce apply` re-scans the Steam library on each run, but the curated lists never become PlayTime masks (use `games.extra_masks`). |
| `domains_starter.yaml` | Next web-block tick, next notify-tick and next `classify` run. The capture loop and the dashboard do not read it (no `config_file` use in either). |
| `title_brands.yaml` | Next process start of each consumer (per tick for the timers). |
| `dashboard.env` | `sudo systemctl restart familymon-dashboard.service` (the app reads the variables once in `create_app`). |

Policy files are re-read by every command on every run. No process caches `policy.yaml`.

`familymonctl policy set` (and the `policy_set` MCP tool) applies nothing; it reports, for each changed top-level key, what makes the change take effect, from this table (`takes_effect` in the result):

| Changed key | Reported as |
|---|---|
| `user`, `lockout`, `allowed_hours`, `daily_limits` | `enforce plan, then enforce apply (or the hourly reconciler)` |
| `games` | the same, plus `charge_browser_games* take effect at the next chargeback tick` |
| `web_block` | `live at the next web-block tick (every minute)` |
| `retention` | `next retention run (daily); preview with retention --dry-run` |
| `personal_time` | `live within a minute (capture and notify re-read it every tick)` |
| anything else | `unknown key` (familymon ignores it; usually a typo) |

`familymonctl config set <name>` (and `config_set`) reports one `takes effect` text per file, matching the rows above: `schedule_extras.yaml` not live (run `schedule refresh`), `messaging_apps.yaml` next capture tick, `game_apps.yaml` next game-library scan or a capture restart, `domains_starter.yaml` next web-block tick, notify tick and classify run, `title_brands.yaml` next process start of each consumer.

## 3. `policy.yaml`

Location: `state_dir()/policy.yaml` (production: `/var/lib/familymon/policy.yaml`). Template: `/usr/share/familymon/policy_example.yaml`.

Loading:

- Must be a YAML mapping. An empty file or non-mapping raises `PolicyError("policy file is not a mapping: ...")`.
- A missing file gives `no policy at ...` from `enforce apply|plan`, `chargeback` and `web-block`.
- Unknown top-level keys are silently ignored. `policy validate` does not flag them either; `policy set` reports a changed unknown key as `unknown key`.
- `familymonctl policy validate` runs every parser that reads the file (enforcement, `games.charge_browser_games_when`, `personal_time`, `web_block`, `retention`) and reports one error per failing section.

### 3.1 Top-level keys

| Key | Type | Allowed / default | Effect | Applied by |
|---|---|---|---|---|
| `user` | str | Required. Missing, empty or non-str raises `policy needs a 'user'`. | Account to govern. The account must exist or `enforce` fails with `policy user ... does not exist`. | all `enforce` commands |
| `lockout` | str | `lock`, `suspend`, `suspendwake`, `terminate`, `kill`, `shutdown`. Default `lock`. Case-insensitive. | timekpr lockout type when time or hours run out. | `enforce apply` |
| `allowed_hours` | per-day map of `{start, end}` | Required. See 3.3. | timekpr allowed hours. | `enforce apply` |
| `daily_limits` | per-day map of durations | Required. See 3.2, 3.4. | timekpr daily time budget. | `enforce apply` |
| `games` | mapping | Optional (`null` becomes `{}`). Non-mapping raises. See 3.5. | PlayTime (game time) enforcement and browser-game chargeback. | `enforce apply`, `enforce chargeback` |
| `web_block` | mapping | Optional. Absent means disabled. See 3.6. | Lesson-time web blocking. | `enforce web-block` |
| `retention` | mapping | Optional. `screenshots_days`: whole number, absent = 90, `0` = keep forever. See 3.9. | Screenshot retention. | `retention` |
| `personal_time` | per-day map of `{from, until}` | Optional. Absent or `off` = none. See 3.10. | Evening window with no screenshots and no titles on the dashboard; game time still counts. | capture supervisor, dashboard, `schedule notify-tick` |

Parent decides: `lockout`, hours, limits, which categories to block. Safety: `terminate`, `kill` and `shutdown` end the session abruptly.

### 3.2 Per-day maps

Used by `allowed_hours`, `daily_limits` and `games.daily_limits`.

- A non-dict scalar applies to all 7 days. Valid for durations (`daily_limits: 2h`). Rejected for `allowed_hours` (it requires a `{start, end}` mapping).
- A dict may use only the keys `default` and the lowercase English names `monday` through `sunday`. Other keys, including capitalised names, raise `unknown day name(s)`.
- The dict needs `default`, or all 7 days. Unlisted days fall back to `default`.
- All 7 days are always allowed. To ban a day, set its limit to `0s`.

### 3.3 `allowed_hours.<day>`

Exactly `{start, end}`, both integers, `0 <= start <= end <= 23`. Whole hours, inclusive: `{start: 7, end: 21}` allows 07:00 to 21:59.

### 3.4 Durations

| Form | Meaning |
|---|---|
| non-negative integer | seconds |
| digit string, e.g. `"5400"` | seconds |
| `XhYmZs`, e.g. `2h`, `1h30m`, `45m`, `90s` | each part optional, at least one required, case-insensitive |
| `unlimited`, `none`, `""` | 86400 seconds |

All values are capped at 86400. Booleans, negatives, floats and unparseable values raise `PolicyError`.

### 3.5 `games`

| Key | Type | Default | Effect | Applied by |
|---|---|---|---|---|
| `enabled` | bool (read via `bool()`) | `false` | Gates PlayTime. When false, PlayTime limits are 0 and there are no activity masks. | `enforce apply` |
| `daily_limits` | per-day map of durations | none | Required only when `enabled` is truthy, else `missing games.daily_limits`. | `enforce apply` |
| `extra_masks` | list of str | `[]` | Extra PlayTime process masks, form `mask` or `mask[Description]`. `;` and nested `[]` are rejected. A mask not starting with `.*` is wrapped as `.*mask.*`. Non-list raises. | `enforce apply` |
| `charge_browser_games` | truthy | falsy | Enables chargeback: browser-game minutes are deducted from PlayTime left. Needs `games.enabled` too. When off, `enforce chargeback` prints "chargeback disabled" and exits normally. | `enforce chargeback` |
| `charge_browser_games_when` | str | `visible` | `visible`: bill a capture whose primary category is `games`. `focused`: also require the focused window's title to classify as games. Other values raise `ClickException`. | `enforce chargeback` |

Notes:

- Steam masks are generated at apply time from the kid's scanned Steam library as `.*common.<installdir>.*`. They are not configured in `policy.yaml`.
- Chargeback bills only captures with `game_running` false and a browser-like window class, up to 1800 seconds per run. It reads the kid's capture DB strictly read-only.
- timekpr prerequisite: PlayTime has no effect until the global PlayTime switches (including the enhanced activity monitor) are enabled in `/etc/timekpr/timekpr.conf`. Familymon does not edit that file. See [troubleshoot](../operate/troubleshoot.md).

### 3.6 `web_block`

Applies during a lesson, which is a `school` block in `schedule.yaml` (breaks suspend blocks). Outside lessons there is no `URLBlocklist`; the base policy, the extension entries and (with `block_private: always`) the private-window policies remain.

| Key | Type | Default | Validation and effect |
|---|---|---|---|
| `enabled` | bool | `false` | Must be a real bool. Master switch. When false the managed Chrome policy file is deleted. |
| `categories` | list of str (a bare str is wrapped) | `[messaging, social, games]` | Entries are lowercased; whitespace inside an entry is rejected. Each must be one of `school, homework, games, social, messaging, video, code, productivity, browsing_other, idle, unknown`. Their domains from `domains_starter.yaml` form the block set. `video` is deliberately not default. Parent decides. |
| `extra_domains` | list of str | `[]` | Added to the block set. A bare host covers its subdomains. |
| `allow_domains` | list of str | `[]` | Added to the allow set and removed from the block set. |
| `forbid_other_browsers` | bool | `true` | Closes other browsers (hard-coded lists of executables and Flatpak ids) during a lesson. |
| `forbid_apps` | list of mask str | `[]` | Extra apps to close during a lesson. Masks are wrapped as `.*mask.*`, must compile as a Python regex, and are fully matched (case-insensitive) against `"<exe> <cmdline>"`. `;` and nested `[]` are rejected. |
| `block_private` | str | `lessons` | `lessons`: block incognito and guest during lessons. `always`: always. `off`: never. Anything else raises. |

Fixed behaviour, not configurable:

- Never blocked: `meet.google.com`, `teams.microsoft.com`, `zoom.us`.
- Chrome started with `--disable-extensions` is closed during a lesson whenever `web_block.enabled` is true, because it would run without the tab-redirect extension.
- Safety: the sweep closes processes of `kid` (SIGTERM, then SIGKILL after 3 s) and requires root. It refuses uid 0. Preview with `familymonctl enforce web-block --dry-run`, which lists what would be closed and writes nothing.
- Chrome URLBlocklist bites on navigation only. Already-open tabs are handled by the force-installed extension (section 9).

### 3.7 Minimal valid example

```yaml
user: kid
lockout: lock
allowed_hours:
  default: {start: 7, end: 21}
  saturday: {start: 8, end: 22}
  sunday: {start: 8, end: 22}
daily_limits:
  default: 2h
  saturday: 4h
  sunday: 4h
games:
  enabled: true
  daily_limits:
    default: 1h
    saturday: 2h
    sunday: 2h
web_block:
  enabled: true
  categories: [messaging, social, games]
```

### 3.8 Apply, preview, verify, undo

| Step | Command | Root | Notes |
|---|---|---|---|
| Read | `familymonctl policy get` | root | The file as it is, comments included. `--json` adds the parsed meaning. |
| Check | `familymonctl policy validate FILE` (or `-` for stdin) | no for your own file | Every parser that reads the file; one error per failing section; exit 1 when invalid. Shows the resolved per-day windows and limits. Show the parent that summary. |
| Write | `familymonctl policy set FILE` (or `-`) | root | Validates again, keeps the old file as `policy.yaml.prev`, writes the whole document verbatim (mode 0600), writes a `policy_changed` event. Applies nothing; prints what makes each changed key take effect (section 2). |
| Preview | `familymonctl enforce plan` | root or `timekpr` group | Prints the timekpra changes and any safety refusal. Never refuses. It may prune expired grants from `grants.json`. |
| Apply | `familymonctl enforce apply` | root or `timekpr` group | Idempotent, writes only deltas, allowed hours last. Writes a `policy_applied` event when something changed. |
| Verify | `familymonctl enforce show` | root or `timekpr` group | Reads live timekpr state over D-Bus for the policy user (or a USER given). |
| Undo | `familymonctl policy set /var/lib/familymon/policy.yaml.prev`, then `plan` and `apply` | root | One level only: each `policy set` overwrites `.prev`. Editing the file by hand also works but keeps no copy. |

The same steps exist as MCP tools (`policy_get`, `policy_validate`, `policy_set`, `enforce_plan`, `enforce_apply`, `enforce_show`); see [mcp.md](mcp.md). Run each step separately and read its output before the next.

Root: timekpr's admin surface needs root or `timekpr` group membership (same for `enforce grant`, `grants`, `revoke`). A non-root run uses its own `state_dir()`, so it reads a different `policy.yaml` and `grants.json` unless `FAMILYMON_STATE_DIR` is set.

Safety guard: while `kid` has a logind session, `apply` refuses (exit code 3) if today's new window excludes the current time, or if today's daily limit would be lower than the live one. `--force` overrides. The limit check is conservative and ignores time already spent. If loginctl fails, a session is assumed. Apply during hours that stay allowed, or when `kid` is logged out. Do not use `--force` without the parent's decision.

One-off extensions: use grants (`familymonctl enforce grant ...`), never raw `timekpra --setallowedhours`. Grants are date-bound, stored in `grants.json`, widen only today's hours, and expire on their own. The hourly reconciler reverts manual edits.

### 3.9 `retention`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `screenshots_days` | whole number | 90 | Screenshot files in day directories dated older than this are deleted once a day by `familymon-retention.timer`; `0` keeps them forever. Capture rows and classifications are not affected; their `screenshot_path` is cleared. |

Preview with `sudo familymonctl retention --dry-run`. The job reads the key on every run; no `enforce apply`.

### 3.10 `personal_time`

Per-day map (3.2) of `{from: "HH:MM"}` or `{from: "HH:MM", until: "HH:MM"}`; a day set to `off` has none. A bare `{from: "20:00"}` applies to every day. Absent or `off` disables it. Times must be quoted strings.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `from` | `"HH:MM"` | required | Start of personal time on that day. |
| `until` | `"HH:MM"` | the bedtime lock | End, exclusive. Default: the minute after the day's last allowed hour (`allowed_hours`, widened by any active grant from `enforce grant --until`), so allowed through 21:59 means 22:00. Must be after `from`. A `from` at or after the end means no personal time that day. |

While the window is in effect, every tick: the probe takes no screenshot (skip reason `personal_time`; a locked or inactive session keeps its own reason and still classifies as `idle`), window class and title are recorded as usual, and the capture row is flagged `personal_time`. The classifier and `enforce chargeback` treat the row like any other, so browser-game minutes keep counting. The dashboard and `/api/captures` return the category and the time span but `null` for app, title, evidence and skip reason, and tag the span "personal time"; `/api/stats` counts the categories normally. `familymonctl personal-time` shows the day's window and the hidden counts. The kid gets one desktop notice per day while the window is in effect (`schedule notify-tick`).

Live: the capture supervisor and the notify tick re-read the key every minute. No `enforce apply`. An unusable section is logged by the supervisor (`personal time: policy unusable`), reported by `personal-time`, and monitoring continues in full. Not masked: `familymonctl conflicts` and the live lesson warning. The window is meant for the evening, after lessons.

## 4. `schedule.yaml`

Location: `state_dir()/schedule.yaml`, which in production is `/var/lib/familymon/schedule.yaml`. Normally written by `schedule import` / `schedule refresh`, but it can be written by hand. No importer is required. All times are the kid's local time.

Loading: an empty file loads as an empty schedule. A non-mapping top level raises a raw `AttributeError`.

### 4.1 Top-level keys

| Key | Type | Notes |
|---|---|---|
| `timezone` | str or null | Stored and displayed only. No logic uses it. |
| `blocks` | list of blocks | Optional. |
| `breaks` | list of breaks | Optional. |

### 4.2 Block keys

| Key | Type | Default | Notes |
|---|---|---|---|
| `day` | str | required | Full English day name, case-insensitive. An invalid name raises `KeyError` when first used. |
| `start` | `"HH:MM"` str | required | Always quote. PyYAML parses an unquoted `10:15` as the integer 615 and loading then fails (`09:00` happens to stay a string, but do not rely on that). |
| `end` | `"HH:MM"` str | required | Exclusive (`start <= t < end`). `Schedule.load` does not check `end > start`. |
| `subject` | str | `""` | Label for display, notifications and web-block. |
| `category` | str | `school` | Not validated. Only `school` blocks are conflict-monitored and trigger web blocking. |
| `group_name` | str or null | none | Added as a `school` title brand for classification. |
| `notify` | bool | `false` | Desktop notification at block start, for any category. |

The first matching block wins.

### 4.3 Break keys

| Key | Type | Default | Notes |
|---|---|---|---|
| `start` | date | required | ISO date (`2026-10-19`). |
| `end` | date | required | Inclusive. `end < start` raises `ValueError`. |
| `name` | str | `""` | Label. |
| `categories` | str or list | `["school"]` | Blocks of these categories are suspended on those dates. An empty list raises. Not validated against the category list. |

A break suspends matching blocks without deleting them. It affects web-block, live warnings, reminders and the dashboard.

### 4.4 Hand-written example

```yaml
timezone: Europe/Berlin
blocks:
  - {day: Monday, start: "09:00", end: "10:00", subject: Maths, category: school, notify: true}
  - {day: Monday, start: "10:15", end: "11:15", subject: English, category: school}
breaks:
  - {name: Autumn break, start: 2026-10-26, end: 2026-10-30}
```

Caveats:

- `schedule import` overwrites the whole file. Both `import` and `refresh` replace `breaks` with the ones from `schedule_extras.yaml`, so hand-written breaks in `schedule.yaml` are lost.
- `schedule refresh` rebuilds the file from the existing `school` blocks plus `schedule_extras.yaml`. A hand-written `school` block survives. A hand-written non-`school` block is dropped unless it is also in `schedule_extras.yaml`.
- Preview with `familymonctl schedule show` (read-only).

## 5. `schedule_extras.yaml`

Resolved via section 1.1 (normally `/etc/familymon/schedule_extras.yaml`, a conffile). Consumed only by `schedule import` and `schedule refresh`. Edits are not live: run `sudo familymonctl schedule refresh`. Read it with `familymonctl config get schedule_extras.yaml` and replace it with `sudo familymonctl config set schedule_extras.yaml FILE` (or `-`), which runs this loader before writing and keeps the previous override as `schedule_extras.yaml.prev`. A missing file is treated as empty. A non-mapping top level raises `ValueError`. Unknown keys are ignored.

| Key | Type | Default | Effect |
|---|---|---|---|
| `exclude_subjects` | list of str | `[]` | Drops imported lessons whose `subject` matches (stripped, case-insensitive). |
| `blocks` | list of blocks | `[]` | Extra blocks, same shape as 4.2. `end` must be after `start`, else `ValueError`. Duplicates are not added. The result is sorted by day and start. |
| `breaks` | list of breaks | `[]` | Same shape as 4.3. They replace the schedule's breaks (not merged). |

Timetable importer (public feature): `familymonctl schedule import FILE --format kingsinterhigh` (the only format). Input is the JSON from the school student API `.../timetable/week`. It uses `timetableLessons[].lessonDay`, `observedStartTime`, `observedEndTime`, `subjectName`, `groupName` and the top-level `timezone`. Imported lessons get `category: school`. Breaks are not imported from the API. The import is manual, per term.

```yaml
exclude_subjects: [Tutor Time]
blocks:
  - {day: Friday, start: "14:00", end: "15:00", subject: Study hall, category: school}
breaks:
  - {name: Autumn break, start: 2026-10-26, end: 2026-10-30}
```

## 6. `messaging_apps.yaml` (privacy contract)

Conffile `/etc/familymon/messaging_apps.yaml`. Window metadata is always recorded. For every visible matching window (not only the focused one) the capture backend blacks out that window's region in the screenshot. If the region cannot be located safely, the whole screenshot is skipped. An unredacted messaging window is never persisted as an image.

| Key | Type | Effect |
|---|---|---|
| `window_classes` | list of str | Lowercased. A window is skipped if its lowercased class, or any underscore-separated segment of it, matches. |
| `title_patterns` | list of regex | Compiled case-insensitive, matched with `.search` on the window title. An invalid regex raises `re.error` at load. |

Both keys are optional. An empty file raises `AttributeError` (no `or {}` guard), so keep at least `window_classes: []`; `config set messaging_apps.yaml` rejects an empty file and an invalid regex before writing. Edit with `config get` / `config set` (previous override kept as `messaging_apps.yaml.prev`). Apply: next capture tick, or `sudo systemctl restart familymon-capture.service`.

Safety: weakening this list exposes private messages to screenshots. Do not remove entries without the parent's decision.

```yaml
window_classes: [whatsapp, signal]
title_patterns:
  - '^(?:\(\d+\)\s*)?WhatsApp\s+[—–-]\s+'
```

## 7. `game_apps.yaml`

Conffile `/etc/familymon/game_apps.yaml`. A curated list that supplements the installed Steam library scan for game detection. Signals come from installed or system state, never from anything the kid controls.

| Key | Type | Effect |
|---|---|---|
| `window_classes` | list of str | Lowercased. Matched against the whole window class or any underscore segment. |
| `binaries` | list of str | Lowercased. Matched against the basename of the window process's executable path or of any of its command-line arguments. |

An empty file gives empty lists (an unreadable file also does; the resolver always finds at least the bundled copy). Parse errors are not caught at load; `config set game_apps.yaml` checks first that only `window_classes` and `binaries` are present, each a list of strings, and keeps the previous override as `game_apps.yaml.prev`. The bundled file already lists common launchers and emulators; an override replaces it entirely, so start from `familymonctl config get game_apps.yaml`. This example uses placeholder names. Apply: `sudo systemctl restart familymon-capture.service` (or wait for the 30-minute rescan). `enforce apply` re-scans on every run.

```yaml
window_classes: [example-launcher]
binaries: [example-game-bin]
```

## 8. `domains_starter.yaml` and `title_brands.yaml`

Both are mappings of `category -> list`. Overrides go in `/etc/familymon/<name>` or `$FAMILYMON_CONFIG_DIR`. Start from the file in effect (`familymonctl config get <name>`), then edit, because an override replaces the bundled file entirely. Write it with `sudo familymonctl config set <name> FILE` (or `-`): it rejects a key that is not a known category and a value that is not a list of strings, and keeps the previous override as `<name>.prev`.

| File | Value list | Matching | Consumers |
|---|---|---|---|
| `domains_starter.yaml` | domains | Apex suffix (`m.youtube.com` matches `youtube.com`). Lowercased, trailing `.` stripped. First occurrence of a domain wins. | classifier, `enforce web-block` (block set) |
| `title_brands.yaml` | brand substrings | Lowercase substring on the window title. First match in file order wins. The schedule's `group_name` values are appended as `school` brands. | `classify`, live warnings, `focused` chargeback |

Rules:

- Bundled categories: `domains_starter.yaml` has `video, messaging, social, games, code, school, productivity`. `title_brands.yaml` has `video, social, messaging, code, school, productivity, games`.
- The classifier skips unknown category names silently (a hand-written override can contain them; `config set` refuses them). `web-block` keeps all keys but only blocks the categories named in `web_block.categories`.
- Empty files are tolerated. A non-mapping raises (`PolicyError` in `web-block`).
- Apply: next tick or process start (see section 2). To relabel history after a change, use `familymonctl classify --redo`, which discards heuristic-sourced classifications and re-runs the heuristics over the whole history (VLM and bootstrap labels are kept).

```yaml
games:
  - example-game.test
social:
  - example-social.test
```

Parent decides: category membership of a domain changes what is blocked and how time is reported.

## 9. Files familymon writes (do not hand-edit)

### 9.1 Chrome managed policy

`/etc/opt/chrome/policies/managed/familymon.json`, written by `enforce web-block` (root). It is a pure function of policy, schedule and current time, written atomically via a temp file in the parent directory, only on change, and deleted when `web_block.enabled` is false. Other files in that directory are never touched. Undo: set `web_block.enabled: false` and wait for the next tick, or run `familymonctl enforce web-block` as root.

| Key | Value | Present when |
|---|---|---|
| `DnsOverHttpsMode` | `"off"` | `web_block.enabled` |
| `EncryptedClientHelloEnabled` | `false` | `web_block.enabled` |
| `DeveloperToolsAvailability` | `0` | `web_block.enabled` |
| `IncognitoModeAvailability` | `1` | private windows blocked per `block_private` |
| `BrowserGuestModeEnabled` | `false` | private windows blocked |
| `URLBlocklist` | sorted domains | only during a lesson |
| `URLAllowlist` | never-block hosts plus `allow_domains` | with `URLBlocklist`, if non-empty |
| `ExtensionInstallForcelist` | `["<id>;<update_url>"]` | the package ships the extension |
| `3rdparty.extensions.<id>` | managed storage object (9.2) | same |

### 9.2 Extension managed-storage schema

Defined in `familymon/chrome_extension/schema.json`. No required keys.

| Property | Type | Written as |
|---|---|---|
| `active` | boolean | `true` when the block set is non-empty |
| `blocked` | array of string | the block set |
| `allowed` | array of string | the allow set, `[]` when nothing is blocked |
| `subject` | string | block subject, or `""` |
| `until` | string | block end as `HH:MM`, or `""` |

The extension (Manifest V3, Chrome 120 or later, permissions `tabs`, `storage`, `alarms`) redirects matching tabs to its own `blocked.html` page, on policy change, install, startup, tab update and a 1-minute alarm. It does not run in private windows, hence `block_private`. If the package ships no extension, the extension keys are omitted.

## 10. `dashboard.env`

Create `/etc/familymon/dashboard.env`, owner root:root, mode 0600 (the package's postinst forces that mode if the file exists). It is a systemd environment file, one `KEY=value` per line, read by systemd as root before `familymon-dashboard.service` drops to the `familymon` user, so neither the dashboard process nor the kid can read the password. Not shipped, not a conffile. Without it the unit is skipped.

```
FAMILYMON_DASH_USER=parent
FAMILYMON_DASH_PASS=change-me
FAMILYMON_DASH_BIND=192.0.2.10
FAMILYMON_DASH_PORT=8765
```

Safety: the default bind is `127.0.0.1`. Binding to `0.0.0.0` or a LAN address exposes captured activity to that network behind basic auth only. Parent decides. Apply: `sudo systemctl restart familymon-dashboard.service`.

## 11. Common mistakes

- Editing `policy.yaml` under `/etc/familymon` or `$FAMILYMON_CONFIG_DIR`: ignored. It lives in the state dir.
- Sending a fragment (only the key you changed) to `policy set` or `config set`: the document replaces the whole file. For `policy.yaml` a fragment usually fails validation (`user`, `allowed_hours` and `daily_limits` are required); one that passes silently drops every other section. Read the whole file with `policy get` or `config get`, edit it, send the whole file back (comments are kept verbatim).
- Expecting `enforce apply` to pick up `web_block` or chargeback keys: those run on their own timers.
- Expecting `schedule_extras.yaml` edits to be live: run `schedule refresh`.
- Unquoted `HH:MM` times in schedules.
- Misspelled top-level keys in `policy.yaml`: silently ignored.
- Empty `messaging_apps.yaml`: raises at load.
- Assuming the enforce, chargeback and web-block timers are off: the package enables them, so they act as soon as `policy.yaml` exists. Preview with `enforce plan` and the `--dry-run` forms before saving (see [files and units](files-and-units.md)).
