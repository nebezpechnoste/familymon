# Configure from intent

**Read this when** the parent has told you, in any form (words, a PDF or screenshot of a timetable, a school-portal export), what the kid may and may not do, and you must turn that into familymon configuration. familymon has no settings UI. You are the UI. This file is the playbook: what to ask, where each kind of intent lives, worked examples with preview and verification, and the rules you follow. Exact option lists are in [../reference/cli.md](../reference/cli.md) and [../reference/config.md](../reference/config.md). Paths and units are in [../reference/files-and-units.md](../reference/files-and-units.md). Background on categories and enforcement is in [../concepts.md](../concepts.md). After applying, run the checks in [verify-and-health.md](verify-and-health.md). If something does not behave, go to [troubleshoot.md](troubleshoot.md).

Conventions: monitored account `kid`, parent "the parent", laptop `kid-laptop`, timezone `Europe/Berlin`. Commands run on `kid-laptop`.

## 0. Ground rules (summary)

1. **Parent decides** every policy choice. You translate, preview and explain. See section 5.
2. Establish facts from data before proposing a change. Separate a **bug** (code did not do what the rule says) from a **policy question** (the rule itself). Never change classification or enforcement policy from one ambiguous data point.
3. Always preview, then apply, then verify. Show the parent the resulting plan in plain words.
4. **Safety:** never weaken monitoring because the kid asked. Monitoring is disclosed to the kid, and the parent owns the rules.
5. **Safety:** never take a raw screenshot of the kid's session to check a result. It bypasses the messaging redaction. Verify through CLI output, the desktop notification history or D-Bus state, or familymon's own redacting capture.

## 1. Where configuration lives

| What | File | Who writes it | How a change takes effect |
|---|---|---|---|
| Time, game and web-block policy | `policy.yaml` in the state dir. Root run: `/var/lib/familymon/policy.yaml`. Template: `/usr/share/familymon/policy_example.yaml` | root | `user`, `lockout`, `allowed_hours`, `daily_limits`, `games.enabled`, `games.daily_limits`, `games.extra_masks`: `familymonctl enforce apply` (or the hourly `familymon-enforce.timer`). `games.charge_browser_games*`: next `familymon-chargeback.timer` tick. `web_block.*`: next `familymon-webblock.timer` tick (every minute) |
| Weekly lesson schedule | `/var/lib/familymon/schedule.yaml` | root (`sudo familymonctl schedule import` or `refresh`) or a hand-written file | Read live by every consumer. No restart |
| Parent extras for the schedule | `/etc/familymon/schedule_extras.yaml` (dpkg conffile) | root | Only when `familymonctl schedule refresh` (or `schedule import`) is run (as root). Edits alone are not picked up |
| Redaction list | `/etc/familymon/messaging_apps.yaml` | root | Next tick: every capture probe is a new process and loads the list when it starts |
| Curated game list | `/etc/familymon/game_apps.yaml` | root | Re-read when the supervisor scans the game library (at start and every 30 min); `sudo systemctl restart familymon-capture.service` applies it at once. This list does not feed timekpr: the PlayTime masks come from the kid's installed Steam library plus `games.extra_masks`, rescanned on each `enforce plan`/`apply` |
| Domain categories, title brands | bundled `domains_starter.yaml`, `title_brands.yaml`. Override by creating `/etc/familymon/<name>` (not installed by default) | root | Next tick of the consumer (web-block every minute, `classify` every 5 minutes) |

Facts that cause mistakes:

- `policy.yaml` and `schedule.yaml` do **not** honour `/etc/familymon` or `$FAMILYMON_CONFIG_DIR`.
- `policy.yaml` has **no unknown-key detection**. A typo such as `web_blocks:` is silently ignored and web blocking stays off. Always confirm the effect with a preview.
- There is one schedule file, `/var/lib/familymon/schedule.yaml`, and every consumer (web-block, notify, conflicts, the dashboard) reads it. All schedule commands run as root (`sudo familymonctl schedule ...`). `enforce web-block --schedule FILE` can still evaluate a different file.
- Always quote times in YAML (`"09:00"`). An unquoted value may not load as a string.
- Day names in `schedule.yaml` are English (`Monday`). Day keys in `policy.yaml` per-day maps are **lowercase** (`monday`, plus `default`).
- A per-day map needs `default` or all seven days. A bare duration scalar applies to all days (valid for `daily_limits`, not for `allowed_hours`).

## 2. Intake questions

Ask these before editing. Skip any the parent has already answered. Record the answers in your report to the parent.

| # | Ask | Why / where it lands |
|---|---|---|
| 1 | Which account is monitored? | `user:` in `policy.yaml`. Must exist on the machine |
| 2 | Which timezone? | `timezone:` in `schedule.yaml` (label only). All times are the kid's local time |
| 3 | Where does the school timetable come from? A Kings InterHigh student API export, a PDF, a screenshot, a portal export, or typed? | Importer (`schedule import --format kingsinterhigh`) or hand-written `schedule.yaml` |
| 4 | Which subjects does the kid not attend, and are there extra routine items (self-study, reading)? | `exclude_subjects`, extra `blocks` |
| 5 | During lessons, which categories are blocked and which are spared? Default blocked: messaging, social, games. Is video spared? | `web_block.categories` |
| 6 | Any specific sites to always allow or block in lessons? Any non-browser apps to close? | `allow_domains`, `extra_domains`, `forbid_apps` |
| 7 | Game budget per day type (school day, weekend). Are native games the only concern, or also browser games? | `games.*` |
| 8 | Total device time per day type? | `daily_limits` |
| 9 | Bedtime (last allowed time) per day type. Is Sunday a school night? | `allowed_hours` |
| 10 | What should happen at the limit: lock, suspend, shutdown? | `lockout` |
| 11 | Should the kid get a notification at the start of some blocks, and a warning when a distraction is seen during a lesson? | `notify: true`; warnings come from `schedule notify-tick` |
| 12 | Private browsing (incognito, guest): off during lessons only, always, or not blocked? | `web_block.block_private` |
| 13 | Holidays and breaks in the next term: dates and whether only lessons are suspended. | `breaks` in extras |
| 14 | Confirm the kid knows monitoring is on. | Disclosed monitoring only. Do not proceed to hidden setups |

Ask about **whole-hour limits** explicitly. `allowed_hours` works in whole hours: `{start: 7, end: 21}` allows 07:00 to 21:59. A bedtime of 21:30 cannot be expressed in `policy.yaml`. Offer 21:00 (`end: 20`) or 22:00 (`end: 21`) and let the parent choose. One-off partial-hour extensions are possible with `enforce grant --until`.

## 3. Intent to configuration map

| Parent's intent | File | Key or command | Preview | Needs root |
|---|---|---|---|---|
| Bedtime per day type | `policy.yaml` | `allowed_hours.<default\|monday..sunday>.{start,end}` | `enforce plan` | yes (edit + apply) |
| Wake time / earliest use | `policy.yaml` | `allowed_hours.*.start` | `enforce plan` | yes |
| Total daily device time | `policy.yaml` | `daily_limits` | `enforce plan` | yes |
| Ban a whole day | `policy.yaml` | `daily_limits.<day>: 0s` | `enforce plan` | yes |
| Game minutes per day type | `policy.yaml` | `games.enabled: true`, `games.daily_limits` | `enforce plan` | yes |
| Count browser games against the budget | `policy.yaml` | `games.charge_browser_games: true`, `games.charge_browser_games_when: visible\|focused` | `enforce chargeback --dry-run` | yes |
| Extra game app to count | `policy.yaml` | `games.extra_masks: ["name[Description]"]` | `enforce plan` | yes |
| What happens at the limit | `policy.yaml` | `lockout: lock\|suspend\|suspendwake\|terminate\|kill\|shutdown` | `enforce plan` | yes |
| Block distractions during lessons | `policy.yaml` | `web_block.enabled: true`, `web_block.categories` | `enforce web-block --dry-run` | edit: yes |
| Spare a category (for example video) | `policy.yaml` | omit it from `web_block.categories` | `enforce web-block --dry-run` | yes |
| Always allow a site in lessons | `policy.yaml` | `web_block.allow_domains` | `enforce web-block --dry-run` | yes |
| Always block a site in lessons | `policy.yaml` | `web_block.extra_domains` | `enforce web-block --dry-run` | yes |
| Close a non-browser app in lessons | `policy.yaml` | `web_block.forbid_apps` | `enforce web-block --dry-run` | yes |
| Private browsing | `policy.yaml` | `web_block.block_private: lessons\|always\|off` | `enforce web-block --dry-run` | yes |
| Lesson timetable (Kings InterHigh) | `schedule.yaml` | `sudo familymonctl schedule import FILE` | `schedule show` | no |
| Lesson timetable (PDF, screenshot, typed) | `schedule.yaml` | hand-written `blocks` | `schedule show` | no |
| Subject the kid does not attend | `schedule_extras.yaml` | `exclude_subjects` then `schedule refresh` | `schedule show` | edit: yes |
| Extra routine block, no conflict monitoring | `schedule_extras.yaml` | `blocks` (non-`school` category) then `schedule refresh` | `schedule show` | edit: yes |
| Notification at block start | `schedule.yaml` or extras | `notify: true` on the block | `schedule notify-tick --test` for delivery only | no |
| Half-term or holiday | `schedule_extras.yaml` | `breaks` then `schedule refresh` | `schedule show` | edit: yes |
| One-off later bedtime tonight | grants | `enforce grant kid --until HH:MM` | `enforce grants`, `enforce show kid` after | yes |
| Extra game minutes today | grants | `enforce grant kid --playtime 20m` | none (see example 9) | yes |
| Warn but do not block | `policy.yaml` | leave the category out of `web_block.categories` (live warnings still fire) | `enforce web-block --dry-run` | yes |
| Show the parent what happened | CLI | `sudo familymonctl conflicts [--day YYYY-MM-DD]` (reads the schedule and capture DB in `/var/lib/familymon`), dashboard | read-only | no |

Not configurable: the always-allowed hosts `meet.google.com`, `teams.microsoft.com`, `zoom.us`. If the parent asks to change them, say it is not possible in the current code.

## 4. Worked examples

Common preparation for edits to `policy.yaml` (root):

```bash
sudo test -f /var/lib/familymon/policy.yaml || sudo install -m 0600 /usr/share/familymon/policy_example.yaml /var/lib/familymon/policy.yaml
sudo cp /var/lib/familymon/policy.yaml /var/lib/familymon/policy.yaml.bak-$(date +%F)
sudo "${EDITOR:-nano}" /var/lib/familymon/policy.yaml
```

The file mode used above is a suggestion, not a requirement found in the code. The `.bak-` copy is only for your own rollback. familymon never reads it. Each enforce command re-reads `policy.yaml` on every run, so there is nothing to reload.

### Example 1: Bedtime by school night and weekend

Parent: "Bedtime is 21:00 on school nights and 23:00 on Friday and Saturday. They get up at 07:00."

Facts first: ask whether Sunday night is a school night (assumed yes) and confirm whole-hour rounding.

Diff in `policy.yaml`:

```yaml
allowed_hours:
  default: {start: 7, end: 20}    # Sun-Thu: last allowed minute 20:59
  friday:  {start: 7, end: 22}    # ends 22:59
  saturday: {start: 7, end: 22}
```

Plain-words plan for the parent: "Sunday to Thursday the laptop is usable 07:00 to 20:59. Friday and Saturday 07:00 to 22:59." Point out that the end is a whole hour, so 21:00 bedtime means the last allowed minute is 20:59.

Preview and apply:

```bash
sudo familymonctl enforce plan
sudo familymonctl enforce apply
```

Safety: if the kid is logged in and the new window for today excludes the current time, or today's limit would shrink, `enforce apply` refuses with exit code 3. Do not add `--force` unless the parent says to cut them off now. Otherwise wait for logout or let the hourly timer retry.

Verify: `sudo familymonctl enforce show kid` prints per-day allowed spans. Compare to the plan. Run `sudo familymonctl enforce plan` again and expect "live state matches policy". Undo: restore the `.bak-` file and apply again.

### Example 2: Weekday and weekend game budgets

Parent: "One hour of games on school days, three on weekends. Total screen time 4 hours on school days, 7 at weekends."

```yaml
daily_limits:
  default: 4h
  saturday: 7h
  sunday: 7h
games:
  enabled: true
  daily_limits:
    default: 1h
    saturday: 3h
    sunday: 3h
```

Prerequisite (timekpr-next, outside familymon; observed with 0.5.10): PlayTime only takes effect once the global switches in `/etc/timekpr/timekpr.conf` are on: `TIMEKPR_PLAYTIME_ENABLED = True` and `TIMEKPR_PLAYTIME_ENHANCED_ACTIVITY_MONITOR_ENABLED = True` (the enhanced monitor matches command lines, which Wine/Proton and Flatpak games need). Without them the per-user enable is silently masked. Check with `grep PLAYTIME /etc/timekpr/timekpr.conf`; after changing them, `sudo systemctl restart timekpr`. familymon never edits this file.

Preview: `sudo familymonctl enforce plan`. Expect changes to time limits, PlayTime enabled, PlayTime limits and activities. Apply: `sudo familymonctl enforce apply`.

Plain words: "Games get 1h on Mon to Fri and 3h on Sat and Sun, counted by timekpr against the kid's installed Steam games plus any `games.extra_masks` (the curated `game_apps.yaml` list is used for capture detection, not for these masks). Total time is capped at 4h or 7h."

Verify: `sudo familymonctl enforce show kid` shows playtime enabled, per-day limits and `activity:` lines. timekpr applies changes on its next poll (about 3 seconds), so read back after a few seconds. Steam game masks are generated at apply time from the kid's installed library, so re-run `enforce apply` after a new game is installed. Parent decides the budget. Do not raise a budget because the kid reports it is too low.

### Example 3: Block messaging, social and games during lessons, spare video

Parent: "During lessons no chat, no social media, no games. YouTube is fine, it is sometimes used for class."

```yaml
web_block:
  enabled: true
  categories: [messaging, social, games]   # video is not listed, so it is spared
  block_private: lessons
```

These are the defaults for `categories` and `block_private`. Listing them makes the intent explicit.

Prerequisites: a schedule must exist (examples 4 and 5), the `familymon-webblock.timer` system timer must be enabled (shipped, but the package postinst does not enable it, so run `sudo systemctl enable --now familymon-webblock.timer`), and Chrome is the governed browser.

Preview (writes nothing, but run it as root: a non-root run looks for `policy.yaml` in the invoking user's own state dir):

```bash
sudo familymonctl enforce web-block --dry-run
```

Read the output: the block set, the desired JSON, whether a change is needed, and "forbidden apps running now (closed only during a lesson)". Outside a lesson the block set is not applied, so also preview with a schedule that has a lesson now using `--schedule FILE` if you must see the lesson state.

Apply: the next minute tick writes `/etc/opt/chrome/policies/managed/familymon.json`. To force a tick as root: `sudo familymonctl enforce web-block`.

Safety: during a lesson, the tick closes other browsers and Chrome started with `--disable-extensions` (SIGTERM, then SIGKILL after 3 seconds). Tell the parent and kid beforehand. Tabs that were already open are closed by the force-installed extension, not by the URL policy, but only if the install packages that extension (the dry-run line starting `open-tab closer:` says whether it does).

Verify: dry-run shows `change needed: no` after the tick. Whenever the policy file changes (lesson start or end) a `web_block` Event row is written; closing forbidden processes writes a `lesson_close` Event. Check the file exists with `ls -l /etc/opt/chrome/policies/managed/familymon.json`. Do not screenshot the kid's browser. Undo: set `enabled: false`. The next tick deletes the policy file.

Parent decides whether video is blocked. Video in a lesson is still flagged by live warnings (example 12) even when not blocked.

### Example 4: Timetable delivered as a PDF or screenshot

Parent: sends a PDF or a photo of the timetable.

There is no importer for PDFs. Transcribe it into `schedule.yaml` by hand, as root, then read it back to the parent.

1. Extract each lesson as day, start, end, subject. If any cell is unreadable, **ask**. Do not guess times.
2. Write the file `/var/lib/familymon/schedule.yaml` as root (for example `sudoedit`; make it readable by group `familymon`, mode 0640, so the dashboard can show it):

```yaml
timezone: Europe/Berlin
blocks:
  - {day: Monday,    start: "09:00", end: "09:45", subject: Maths,   category: school}
  - {day: Monday,    start: "10:00", end: "10:45", subject: English, category: school}
  - {day: Tuesday,   start: "09:00", end: "09:45", subject: Science, category: school}
```

Rules: `end` is exclusive. `day` is a full English day name. `category: school` makes the block conflict-monitored and web-blocked. `Schedule.load` does not check `end > start`, so check it yourself. A hand-written `school` block survives `schedule refresh`. An empty file loads as an empty schedule. A file that is not a mapping fails with a raw Python error rather than a friendly message.

Preview and verify:

```bash
sudo familymonctl schedule show
sudo familymonctl enforce web-block --dry-run
```

Show the parent the grouped-by-day output and ask them to confirm against the PDF. The `schedule show` output ends with a block count. Compare it with your count of lessons. Undo: restore the previous file, or run `schedule import` with a supported export (it overwrites the file).

Note: `schedule import` overwrites everything. Do not run it over a hand-written schedule without telling the parent.

### Example 5: Kings InterHigh timetable export

Parent: "Here is the JSON from the school timetable API."

```bash
sudo familymonctl schedule import timetable-week.json --format kingsinterhigh
```

The input is the JSON from `GET /student/api/timetable/week`. All imported lessons get `category: school`. Breaks from the API are not imported. Extras (exclusions, extra blocks, breaks) from `/etc/familymon/schedule_extras.yaml` are applied at the same time. The command prints the block count, school and extra counts, excluded subjects and break lines.

Preview: there is no dry-run flag. Save a copy of the old file first (`sudo cp /var/lib/familymon/schedule.yaml{,.bak-$(date +%F)}`), run the import, then `sudo familymonctl schedule show`. Verify the count and a few lessons with the parent. Re-import each term.

### Example 6: Self-study blocks with notifications

Parent: "Weekdays 14:00 to 15:00 is self-study. Remind them when it starts. Do not treat it as a lesson."

Add to `/etc/familymon/schedule_extras.yaml`. The shipped file already ends with the active lines `exclude_subjects: []`, `blocks: []` and `breaks: []`: replace the matching `[]` line instead of adding a second key of the same name (a duplicate key is silently resolved to the last one by the YAML loader, so one of them would be ignored):

```yaml
blocks:
  - {day: Monday,    start: "14:00", end: "15:00", subject: Self-study, category: study, notify: true}
  - {day: Tuesday,   start: "14:00", end: "15:00", subject: Self-study, category: study, notify: true}
  - {day: Wednesday, start: "14:00", end: "15:00", subject: Self-study, category: study, notify: true}
  - {day: Thursday,  start: "14:00", end: "15:00", subject: Self-study, category: study, notify: true}
  - {day: Friday,    start: "14:00", end: "15:00", subject: Self-study, category: study, notify: true}
```

The category `study` is a free string. Only `school` blocks are conflict-monitored and web-blocked. `notify: true` applies to every category: the desktop notification pops at block start (once per day per block), sent by the per-minute `familymon-notify.timer` (root, delivered into the kid's session; it counts as done only if it was delivered).

If you edit `blocks:` in an existing extras file, keep existing entries. Extras `blocks` must have `end > start`, or the load fails with a ValueError.

Apply and preview:

```bash
sudo familymonctl schedule refresh
sudo familymonctl schedule show
```

`refresh` requires an existing schedule and keeps only `school` blocks plus the extras. Hand-written non-`school` blocks in `schedule.yaml` that are not also in the extras are dropped.

Verify delivery separately: `sudo familymonctl schedule notify-tick --test` sends a sample notification (needs the kid's session bus; prints "no session to notify" if they are not logged in). Check the notification history in Plasma rather than taking a screenshot. If no popup appears, see [troubleshoot.md](troubleshoot.md).

Parent decides whether study time should be enforced like a lesson. If yes, use `category: school`, and say so in the plan, because then web blocking applies to that block.

### Example 7: A subject the kid does not attend

Parent: "They do not take French. Ignore those lessons."

```yaml
# /etc/familymon/schedule_extras.yaml (replace the existing `exclude_subjects: []` line)
exclude_subjects:
  - French
```

Matching is case-insensitive on the block `subject`, so the string must equal the subject as it appears in `schedule show` (ignoring case). Confirm the exact spelling from `schedule show` first.

```bash
sudo familymonctl schedule show      # note French blocks
sudo familymonctl schedule refresh
sudo familymonctl schedule show      # French blocks gone
```

Verify: the block count dropped by the number of French lessons. Undo: remove the entry and run `schedule import` again, because `refresh` cannot bring excluded lessons back (they are removed from the file). Tell the parent this before applying.

### Example 8: Half-term break

Parent: "No lessons 26 to 30 October."

```yaml
# /etc/familymon/schedule_extras.yaml (replace the existing `breaks: []` line)
breaks:
  - {name: Autumn half-term, start: 2026-10-26, end: 2026-10-30}
```

`end` is inclusive. `categories` defaults to `[school]`. The extras `breaks` list **replaces** the schedule's breaks, so include every break the parent wants, not only the new one. The imported lessons stay in the file.

```bash
sudo familymonctl schedule refresh
sudo familymonctl schedule show
```

The output prints a `break:` line for each break. If the break covers today it adds "in progress — no lessons today". During a break, web blocking, live warnings, reminders and conflicts for those categories are suspended, and the dashboard shows a school-break banner.

Plain words: "On 26 to 30 October lessons are not enforced and not monitored for conflicts. Time limits and bedtime still apply." Bedtime is policy, not schedule. If the parent wants a later bedtime in the holiday, that is an `allowed_hours` change (example 1) and a separate decision. Undo: remove the break and run `schedule refresh`.

### Example 9: One-off later bedtime tonight

Parent: "Let them stay up until 22:30 tonight."

Use a grant. Never use raw `timekpra --setallowedhours`: it is persistent, and the hourly reconciler reverts it.

```bash
sudo familymonctl enforce grant kid --until 22:30 --note "movie night"
```

`--from HH:MM` sets the start (default now). `--until` is inclusive. The grant widens today's allowed hours only, expires on its own, and is applied immediately without the safety guard (it only widens). The policy user must be `kid`, otherwise the command errors after the grant is already stored.

There is no dry-run. Preview by stating the effect in words and checking the current state: `sudo familymonctl enforce show kid` and `sudo familymonctl enforce grants`. Verify after: `enforce grants` lists the grant, and `enforce show kid` shows the widened span for today. Undo: `sudo familymonctl enforce revoke kid`. If narrowing now would cut off the active session, revoke deletes the grant but leaves timekpr unchanged until it is safe. Tell the parent.

### Example 10: Extra game minutes today

Parent: "Give them 20 more minutes of games today."

```bash
sudo familymonctl enforce grant kid --playtime 20m --note "finished homework early"
```

This adjusts today's PlayTime counter by +20 minutes (`2h30m`, `45m`, `90s` and plain seconds are accepted). It stores no grant record, and the reconciler never touches that counter, so it does not revert. It probably needs PlayTime to be enabled (example 2); the code only calls `timekpra --setplaytimeleft` and does not check this. `--until` and `--playtime` can be combined in one call.

Preview: none. Check before with `sudo familymonctl enforce show kid` (playtime enabled and limits). Verify after a few seconds (timekpr applies it on its next poll, about 3 seconds). The command prints `+1200s game time today`. An audit Event `grant_playtime` is written. Undo: there is no revoke for this, and `grant` only adds minutes (a negative duration is rejected). Ask the parent whether to accept it.

### Example 11: Count browser games against the game budget

Parent: "Poki and the like should count against their game time."

```yaml
games:
  enabled: true
  daily_limits: {default: 1h, saturday: 3h, sunday: 3h}
  charge_browser_games: true
  charge_browser_games_when: visible   # or focused
```

Parent decides `visible` vs `focused`. Both modes only bill captures whose focused window is a browser and where no native game process was running. `visible` then bills a capture whose primary category is games, wherever the game is on screen. `focused` additionally requires the focused window's own title to classify as a game. Explain with an example: a game visible beside the focused school page bills under `visible` but not under `focused`.

Preview (read-only, no timekpr calls):

```bash
sudo familymonctl enforce chargeback --dry-run
```

It prints the uncharged browser-game captures and "charged today so far". Only captures where no native game was running are billed. Each tick is capped at 1800 seconds. Requires the `familymon-chargeback.timer` (every 5 minutes; shipped, not enabled by the package). Verify: after a tick, the next dry-run shows fewer uncharged captures. An Event `playtime_chargeback` is recorded. Undo: set `charge_browser_games: false`. Charge-back only deducts and nothing in the code refunds it; `grant --playtime` can add minutes back if the parent agrees.

### Example 12: Warn but do not block

Parent: "Do not block messaging. Just tell them when they are on it during a lesson, and I will see it afterwards."

Remove `messaging` from `web_block.categories`:

```yaml
web_block:
  enabled: true
  categories: [social, games]
```

Live warnings come from `schedule notify-tick` (per-minute root timer, delivering into the kid's session), independently of blocking. During a `school` block, if the active window is messaging, social, a game or streaming video, a notification "`<subject> — lesson in progress`" is shown, with a cooldown of 5 minutes per lesson and category (override with `--warn-cooldown`). The notification text says the sighting is logged, and it shows in `familymonctl conflicts` and the dashboard (derived from the captures, not a separate event row). Streaming video counts only if the title contains a known streaming brand.

Warnings only fire inside `school` blocks. They are disabled by `--no-warn`. The shipped `familymon-notify.service` runs `familymonctl schedule notify-tick` without that flag, so only a locally modified unit would turn them off.

Preview: `sudo familymonctl enforce web-block --dry-run` shows the block set without messaging domains. Verify later with `sudo familymonctl conflicts --day 2026-10-19` as the parent reviews. Do not generate a conflict by opening a chat on the kid's laptop during a lesson to test, unless the parent agrees.

Parent decides if messaging is blocked, warned or untouched. Note that `meet.google.com`, `teams.microsoft.com` and `zoom.us` are never blocked.

### Example 13: Allow one site, close one app, restrict private windows

Parent: "Block everything social in lessons, but their class uses a site on the social list. Also close the Discord app in lessons. No incognito at all."

```yaml
web_block:
  enabled: true
  categories: [messaging, social, games]
  allow_domains: [classroom-tool.example.org]
  forbid_apps: ["discord"]
  block_private: always
```

`allow_domains` removes the host from the block set. `forbid_apps` entries are masks wrapped as `.*mask.*` and fully matched (case-insensitive) against the process's `exe` plus command line. They must compile as regex, and may not contain `;` or nested `[]`. `block_private: always` sets Chrome's incognito and guest policies at all times, not only during lessons.

Preview: `sudo familymonctl enforce web-block --dry-run`. Check the allow host is absent from the block set, the app is listed under "forbidden apps running now", and `private windows (incognito/guest): always`. Verify as in example 3. Caution: a broad mask can close unrelated processes, so show the parent the matches from the dry-run before applying.

## 5. Rules for the agent

1. **Confirm ambiguous intent.** "Bedtime 9:30", "no games on school days", "the usual holiday" each have several readings. Ask one short question with concrete options. Wording like "after school" needs a clock time.
2. **Separate bug from policy.** If a result surprises the parent, read the data first (`familymonctl conflicts`, `classify` output, dashboard). If the code did not follow the rule, it is a bug, so report it. If the rule is what they dislike, present the trade-off with 2 or 3 examples from the data and let the parent choose. One ambiguous capture is never enough to rewrite a rule. After a real classification policy change, `familymonctl classify --redo` re-labels history (heuristic labels only).
3. **Always preview before apply.** Use the preview column in section 3. Where there is no dry-run (`enforce grant`, `enforce revoke`, `schedule import`, `schedule refresh`), back up the file and describe the effect in words first.
4. **Show the plan in plain words** before applying, and the result after. Use the form: "From tonight: Mon to Thu usable 07:00 to 20:59, games 1h, lesson blocking of messaging/social/games, video allowed. Start 26 Oct: no lessons for a week." Include anything the parent might not expect (rounding to whole hours, sessions closed during lessons).
5. **Never weaken monitoring on the kid's request alone.** Only the parent can: raise a budget, extend hours, spare a category, widen `allow_domains`, edit `messaging_apps.yaml`, disable `web_block`, or switch off a unit. If the kid asks you for such a change, relay it to the parent and do nothing. Do not add anything the kid controls as a switch that pauses monitoring (a game mode flag, a file in their home). Game detection keys on the installed Steam library and the curated list.
6. **Never act on the kid's session beyond the config.** No raw screenshots. Do not kill the kid's processes by hand. Tests and dev runs never touch real processes or user state.
7. **Order of changes for time limits.** Prefer applying when the kid is logged out. The guard refuses windows that exclude now or shrink today's limit. Use `--force` only with explicit parent instruction.
8. **Record what was changed.** familymon keeps no changelog for `policy.yaml` or extras edits. Keep a dated `.bak-` copy of each file before editing, and give the parent a written summary: files, keys, old and new values, preview output, verification result, and the undo step. `enforce grant` has `--note` for one-off grants and records an Event row for the grant.
9. **Verify with real exit codes.** Run the verification as its own step after the apply. Do not pipe a check into the next action. Then follow [verify-and-health.md](verify-and-health.md), including `familymonctl health`.
10. **Typos are silent.** After editing `policy.yaml`, confirm the preview shows the change you intended. A preview that shows "nothing to do" when you expected a change usually means a misspelled key.
11. **Disclosure.** Monitoring is disclosed to the kid. Do not configure anything meant to be hidden from them.

## 6. Open points

- Non-root `timekpr` group members: the `enforce` group help says it needs root or the `timekpr` group, but a non-root run resolves `policy.yaml` and `grants.json` in its own XDG state dir, not `/var/lib/familymon`. These docs assume root.
- A file mode of `0600` for `policy.yaml` is a suggestion. The code does not check it.
