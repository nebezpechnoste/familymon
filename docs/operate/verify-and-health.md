# Verify changes and read health

Read this when you have changed or are about to change anything in familymon (policy, schedule, config YAML, units, package) and must prove it took effect, or when you need to read `familymonctl health`, service state or the event log. Install steps are in [install.md](install.md). Failure handling is in [troubleshoot.md](troubleshoot.md). Command details are in [../reference/cli.md](../reference/cli.md). Paths and units are in [../reference/files-and-units.md](../reference/files-and-units.md).

## Principles

- Establish facts from the data first. Separate a BUG (the code did not do what the rule says) from a POLICY question (the rule itself). Fix bugs. Parent decides policy: present trade-offs with examples and never change classification or enforcement policy on one ambiguous data point.
- Preview, apply, read back. Separate each step into its own call.
- Never gate on a pipe. `pytest | tail && build && publish` hides the exit code and has shipped a red suite. Run the check alone, read the real exit code, then ship in a different step.
- Never take a raw screenshot of the kid's session. It bypasses redaction. Use familymon's own capture, the notification history, or D-Bus state.
- Tests and dev runs never touch real processes or real user state. See [../develop/testing-and-shipping.md](../develop/testing-and-shipping.md).
- Every operator command runs as root (`sudo familymonctl ...`). Heads-up: `health`, `conflicts` and any command that opens the capture DB read-write run `make_engine`, which can create files, fix their permissions, and run recovery. `events`, chargeback and the dashboard open it read-only.

## 1. Which DB am I looking at

There is one: `/var/lib/familymon/captures.db` (root:`familymon`, group-readable). It holds captures, classifications and all events (heartbeats, `capture_failed`, `capture_tamper`, DB recovery, and the enforcement audit rows). Run everything as root:

```bash
sudo familymonctl health
```

A non-root run resolves a different state dir (`~/.local/state/familymon`, dev only) and reports on that. Do not read a non-root `health` as the kid's capture health. If you see captures from before the upgrade, they were moved here by `migrate-state`.

## 2. `familymonctl health`

Read-only apart from the DB-open caveat above. No arguments. Run it at the start and end of every iteration.

Output lines: as of, total captures, last 1h and 24h, last capture age, largest gap 24h, classified 24h, dedup 24h (`n/a` when no captures were eligible, for example locked or idle), messaging full-skips 24h, screenshots (files and MB), categories 24h.

Exit codes:

| Exit | Meaning | Action |
|---|---|---|
| 0 | No privacy violation, DB integrity ok | Read soft warnings anyway |
| 2 | PRIVACY VIOLATION: a capture with a messaging window present has an un-redacted screenshot on disk (counted over all time) | Stop. Do not ship. Investigate redaction; see [troubleshoot.md](troubleshoot.md) |
| 4 | DB integrity not ok even after automatic recovery | Stop. See DB recovery below |

The privacy check runs before the integrity check. Exit 1 and 3 are not set by `health`.

Soft warnings (printed, exit stays 0):

| Warning | Trigger | Likely meaning |
|---|---|---|
| capture is stale | last capture older than 180 s | Capture supervisor stopped or failing, session locked or user logged out |
| gap over 5 min in 24h | largest gap over 300 s | Crash or laptop sleep; check the journal |
| messaging full-skips high | over 10% of 24h captures | Either chats are often foregrounded, or a messaging window has unmappable geometry, so the whole screenshot was skipped. Capture rows and classification are unaffected |
| DB was auto-recovered | a `db_recovered` event exists | Read the event, see below |

Note: a locked or inactive session still records window metadata but skips the screenshot (`session_locked`, `session_inactive`), and classifies as `idle`. A stale warning at night is expected.

## 3. Heartbeat

```bash
sudo familymonctl heartbeat-once      # writes one event
```

It also asks logind whether the monitored user has a graphical session, and records that as `session` (`active`, `locked`, `inactive`, `none`, `unknown`) in the event.

| Status | Meaning | Exit |
|---|---|---|
| `ok` | last capture under 180 s ago | 0 |
| `idle` | the user has no graphical session (`none`) and no fresh capture: nobody to capture, not a fault | 0 |
| `stale` | last capture 180 s to under 600 s ago | 1 |
| `dead` | 600 s or more | 2 |
| `never` | no captures | 3 |

The 1-minute root timer runs it. A non-zero exit marks the unit failed on purpose. Stale or dead while the session is `active` is the signal that capture is broken or being interfered with (see [troubleshoot.md](troubleshoot.md)). One more status exists only on the dashboard: `watcher_down`, shown when the newest heartbeat event is more than 5 minutes old, meaning the watcher timer itself is not running. `heartbeat-once` never prints it. The kid cannot stop the watcher; a gap in `heartbeat` events means the timer was disabled, the machine was off, or something is wrong with the service.

## 4. Service and timer status

Every unit is a system unit:

```bash
systemctl status familymon-capture.service
systemctl list-timers 'familymon-*'
systemctl show familymon-capture.service -p ActiveEnterTimestamp -p ActiveState
journalctl -u familymon-capture.service --since '-30min'
systemctl status familymon-enforce.service familymon-webblock.service familymon-chargeback.service
journalctl -u familymon-enforce.service --since '-2h'
```

Stale code after upgrade: long-running units (capture, dashboard) can keep old code. The deb postinst restarts them. If behaviour looks stale, compare the `ActiveEnterTimestamp` above with the package install time (for example from `/var/log/dpkg.log`). Restart: `sudo systemctl restart familymon-capture.service`.

Interpreting units:
- `familymon-enforce.service` exit 3 and "failed" means the apply guard refused, not that it crashed. See section 6.
- With no `policy.yaml`, the capture, notify, enforce, chargeback and webblock units are skipped by their `ConditionPathExists` (shown as inactive, not failed). With no `/etc/familymon/dashboard.env` the dashboard is skipped the same way.
- The capture supervisor records nothing while the kid has no graphical session. A capture gap after an unclean shutdown self-heals on the next login. Rows classified late appear as `unknown` until the classify timer (every 5 min) or `sudo familymonctl classify` runs. After a classification policy change use `familymonctl classify --redo` (it deletes only heuristic, domain and game_process labels, ignores `--limit`, and recomputes all history).

## 5. Events in the DB

Event kinds the code writes: `heartbeat` (with `status`, `session`, `age_seconds`, `last_capture_at`), `db_recovered`, `capture_failed` (`error`, `session`), `capture_tamper` (`sink_rejected`, `session`), `playtime_chargeback`, `grant_hours`, `grant_playtime`, `web_block`, `lesson_close`. `enforce apply` and `enforce revoke` write no event.

Read them with the CLI (read-only; heartbeats are left out unless you ask):

```bash
sudo familymonctl events                                     # today
sudo familymonctl events --day 2026-10-19
sudo familymonctl events --kind capture_failed --kind capture_tamper
sudo familymonctl events --kind heartbeat --day 2026-10-19
```

Or query the DB directly, read-only (SQLite `mode=ro`):

```bash
sudo sqlite3 -readonly /var/lib/familymon/captures.db \
  "select at, kind, data from event order by id desc limit 20;"
```

Do not write to the DB by hand. Reading it exposes the kid's activity: do it only when needed.

DB self-healing: on open, the DB runs a quick integrity check. If damaged, the file is renamed `captures.db.corrupt-<YYYYmmdd-HHMMSS>` (with `-journal`, `-wal`, `-shm` siblings), then recovered with `sqlite3 .recover`, else Python `iterdump`, else a fresh empty DB. The original is never deleted. A `db_recovered` event records `method`, `finding`, `damaged_backup`, `salvaged_tables`. `health` prints it. Do not delete the `.corrupt-*` file until the parent agrees.

## 6. Verify each kind of change

### Policy (`/var/lib/familymon/policy.yaml`: hours, limits, lockout, games)

```bash
sudo familymonctl enforce plan                 # preview: live vs desired, no writes
sudo familymonctl enforce apply                # root; minimal diff
sleep 5
sudo familymonctl enforce show kid             # read back live state over D-Bus
sudo familymonctl enforce plan                 # must print "live state matches policy"
```

- `plan` exits 0 even when applying would be unsafe. It prints "!! applying NOW would be unsafe". `apply` then exits 3 unless `--force`.
- Safety: the guard refuses when the new window for today excludes the current time or today's limit would shrink, and a session is active. Do not use `--force` to get around it without the parent's decision: it can cut off the kid mid-session. Prefer waiting for logout.
- timekpr timing (timekpr-next behaviour, observed with 0.5.10, not part of familymon's code): timekpr applies a play-time-left change on its next poll, about 3 s. Read back after a few seconds, not immediately.
- Use `enforce show` or D-Bus for booleans. `timekpra --getuserinfo` prints enabled booleans as False because of a CLI bug.
- PlayTime needs the global switches in `/etc/timekpr/timekpr.conf`. If limits are set but nothing happens, check those first.
- PlayTime masks are effectively full-match on the process path or command line. The enforcement driver wraps bare `extra_masks` as `.*...*`. Flatpak apps appear as their `bwrap` wrapper.

### Personal time (`personal_time` in `policy.yaml`)

```bash
sudo familymonctl personal-time                # today's window, in effect now or not, hidden counts
sudo journalctl -u familymon-capture.service --since "1 hour ago" | grep "personal time"
```

- The command must print the window you intended (`20:00–22:00 (ends at the bedtime lock)`); `none` or an "unusable" error means a typo, and monitoring continues in full.
- Inside the window, new capture rows have `personal_time` set and no screenshot: `hidden captures on <day>` grows by about one per minute while the kid is logged in, and `health` shows no new screenshot files for the period.
- The dashboard shows a "Personal time HH:MM–HH:MM" note, category pills with a grey "personal time" tag and no titles for the span. Categories still add up in "Time by category".
- The kid saw one "Personal time" notification (Plasma's notification history, never a raw screenshot). `sudo cat /var/lib/familymon/personal_time_notified.json` holds today's date once it was delivered.
- The journal line `personal time until 22:00: no screenshots` appears at the first tick inside the window and `personal time over: screenshots resume` at the first tick after it.
- Suspend and lock lockouts are enforced only while the session is active (timekpr-next behaviour, observed with 0.5.10): a locked session is never suspended. To grant extra time late at night, wake the screen without unlocking, grant, then unlock.

### One-off extensions (grants)

```bash
sudo familymonctl enforce grant kid --until 21:30 --note "reason"
sudo familymonctl enforce grants
sudo familymonctl enforce show kid
```

Verify a `grant_hours` event exists in the root audit DB and the window shows in `enforce show`. Always use `enforce grant`, never raw `timekpra --setallowedhours`: the raw change is persistent and the hourly reconciler reverts it. `enforce revoke kid` drops today's hour grants and will not narrow timekpr while it would cut an active session (it says so; exit 0).

### Web blocking

```bash
sudo familymonctl enforce web-block --dry-run     # prints desired Chrome policy and "change needed: yes|no"
```

The dry run writes nothing and kills nothing, but it scans processes read-only. After the timer or a real run:
- `cat /etc/opt/chrome/policies/managed/familymon.json` shows the base keys. `URLBlocklist` appears only while a school block is in progress. It is the file Chrome reads. Chrome's `chrome://policy` page shows what Chrome actually loaded.
- A `web_block` event is written when the file changes. A `lesson_close` event is written when processes are closed.
- Incognito and guest follow `web_block.block_private`.
- URLBlocklist only bites on navigation. Open tabs depend on the extension. If the extension seems inactive, check `familymon.crx`, `update.xml`, `id` and `version` exist under `/usr/share/familymon/chrome-extension/`.
- Prerequisite: `web_block.enabled: true` and a schedule (`sudo familymonctl schedule show`). A typo in a top-level key of `policy.yaml` is silently ignored and leaves web blocking disabled, so confirm the dry run shows the config you expect.

### Chargeback

```bash
sudo familymonctl enforce chargeback --dry-run
```

It reports uncharged browser-game captures and today's charged total. The real run is capped at 1800 s per tick and writes a `playtime_chargeback` event.

### Schedule and conflicts

```bash
sudo familymonctl schedule show
sudo familymonctl conflicts            # optionally --day YYYY-MM-DD
```

`schedule show` and `conflicts` print a "no schedule" message with exit 0 when none exists. That is not a success signal. After editing `/etc/familymon/schedule_extras.yaml`, run `sudo familymonctl schedule refresh`, since edits alone are not picked up. Quote every `HH:MM`.

### Classification and capture YAML (apps, games, messaging, domains, brands)

- `domains_starter.yaml`, `title_brands.yaml`: the package does not install these under `/etc/familymon/`; the bundled copies are used unless you place an override there. Overrides are read on each `classify` run (both files) and each web-block tick (`domains_starter.yaml` only). Run `classify --redo` for history, then compare `health` category counts.
- `messaging_apps.yaml`, `game_apps.yaml`: the messaging list is built when each capture probe starts its backend (so it applies on the next tick); the game library is scanned when the supervisor starts and every 30 minutes. Restart `familymon-capture.service` (`sudo systemctl restart ...`) to apply a game list change at once.
- Policy change versus bug: before changing a category rule, list the affected captures, show the parent examples, and state which rule matched (the `evidence_text` column). Do not edit rules from one data point.

### Notifications

`sudo familymonctl schedule notify-tick --test` sends one real popup into the kid's session (warn the kid); it prints "no session to notify" when nobody is logged in. A block-start popup or warning is marked done only when delivery succeeded, so a notice missed because the kid was logged out is retried. Confirm through Plasma's notification history or D-Bus state, never a screenshot. Block-start popups fire once per day per block (`notified.json`). Warnings repeat at most every 5 minutes per lesson and category (`warned.json`).

### Dashboard

```bash
systemctl is-active familymon-dashboard.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/            # expect 401
curl -s -o /dev/null -w '%{http_code}\n' -u 'parent:PASSWORD' http://127.0.0.1:8765/   # expect 200
```

A startup failure usually means `FAMILYMON_DASH_USER` or `FAMILYMON_DASH_PASS` is missing, or the unit was skipped because `/etc/familymon/dashboard.env` does not exist. The env file is read only at start, so restart after changes.

The banner reads the newest heartbeat event: "OK", stale, "Capture DOWN while the session is active", "Not logged in" (idle), "Heartbeat watcher not running" (newest heartbeat older than 5 min), or no captures yet. When the day has `capture_failed` or `capture_tamper` events, a red line says "N capture(s) failed while the session was open; M forged window-list attempt(s) rejected" and points at `sudo familymonctl events --day YYYY-MM-DD`.

### Package upgrade

Check `dpkg -s familymon`, then the units' `ActiveEnterTimestamp` against the install time (section 4), then `sudo familymonctl health`.

## 7. Checklist for every change

Before:
1. `sudo familymonctl health`: exit 0, no unexplained warnings. Record the numbers. Do not apply updates onto an unhealthy capture pipeline.
2. Status of capture and timers (section 4). Capture must be running continuously.
3. Is the kid logged in? (`loginctl list-sessions`.) If yes, expect the apply guard to matter and avoid anything that narrows today's window.
4. State the change as BUG fix or POLICY choice. For policy, get the parent's decision first.
5. Preview: `enforce plan`, `enforce web-block --dry-run`, `enforce chargeback --dry-run`. Commands without a preview (`schedule import`, `schedule refresh`, `enforce grant`, `enforce revoke`): save a copy of the current file or output first (`schedule show`, `enforce show kid`).
6. If code or tests are involved: run the test suite alone and read its exit code. Build and ship in separate calls.

After:
1. Read back (section 6) after waiting about 5 s for timekpr.
2. Confirm the matching event exists, if the command writes one (section 5).
3. `sudo familymonctl health` again: still exit 0, capture not stale, no new `db_recovered`, classification percentage not dropped.
4. Check `systemctl list-timers 'familymon-*'` and failed units: `systemctl --failed`. Check `sudo familymonctl events` for new `capture_failed` or `capture_tamper` rows.
5. Record what changed and how to undo it for the parent.

## 8. Undo reference

| Change | Undo |
|---|---|
| `enforce apply` | Edit `policy.yaml`, `enforce plan`, `enforce apply` |
| `enforce grant` (hours) | `enforce revoke kid`. Grants also expire on their own |
| `enforce grant --playtime` | No revoke command: `revoke` drops hour grants only. The grant is a one-off adjustment of timekpr's play-time-left counter; how it is reset is timekpr behaviour (UNVERIFIED) |
| Web block | Set `web_block.enabled: false`. The next tick deletes `/etc/opt/chrome/policies/managed/familymon.json` |
| `schedule import` or `refresh` | Re-import the timetable, or restore the saved `schedule.yaml` |
| `classify --redo` | Not reversible except by changing rules and running `--redo` again. VLM and bootstrap labels are kept |
| Starting a timer | `sudo systemctl disable --now <timer>` (the package re-enables it on the next upgrade) |
