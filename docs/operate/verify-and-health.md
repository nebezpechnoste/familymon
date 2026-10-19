# Verify changes and read health

Read this when you have changed or are about to change anything in familymon (policy, schedule, config YAML, units, package) and must prove it took effect, or when you need to read `familymonctl health`, service state or the event log. Install steps are in [install.md](install.md). Failure handling is in [troubleshoot.md](troubleshoot.md). Command details are in [../reference/cli.md](../reference/cli.md). Paths and units are in [../reference/files-and-units.md](../reference/files-and-units.md).

## Principles

- Establish facts from the data first. Separate a BUG (the code did not do what the rule says) from a POLICY question (the rule itself). Fix bugs. Parent decides policy: present trade-offs with examples and never change classification or enforcement policy on one ambiguous data point.
- Preview, apply, read back. Separate each step into its own call.
- Never gate on a pipe. `pytest | tail && build && publish` hides the exit code and has shipped a red suite. Run the check alone, read the real exit code, then ship in a different step.
- Never take a raw screenshot of the kid's session. It bypasses redaction. Use familymon's own capture, the notification history, or D-Bus state.
- Tests and dev runs never touch real processes or real user state. See [../develop/testing-and-shipping.md](../develop/testing-and-shipping.md).
- Heads-up: `health`, `conflicts`, the dashboard and any command that opens a capture DB run `make_engine`, which can create files, chmod them, and run recovery. Run them as the owning user (`kid`), not root, so no root-owned files appear in their state dir.

## 1. Which DB am I looking at

`state_dir()` is `$FAMILYMON_STATE_DIR`, else `/var/lib/familymon` for root, else `~/.local/state/familymon`.

| Run as | DB | Holds |
|---|---|---|
| `kid` (or `sudo -u kid -H`) | `~kid/.local/state/familymon/captures.db` | captures, classifications, `heartbeat` and `db_recovered` events |
| root | `/var/lib/familymon/captures.db` | audit events from `enforce` commands only |

A root `familymonctl health` reads the root audit DB, which has no captures. Do not read its output as "capture is down". For capture health:

```bash
sudo -u kid -H familymonctl health
```

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
| capture is stale | last capture older than 180 s | Capture loop stopped, session locked or user logged out |
| gap over 5 min in 24h | largest gap over 300 s | Crash or laptop sleep; check the journal |
| messaging full-skips high | over 10% of 24h captures | Either chats are often foregrounded, or a messaging window has unmappable geometry, so the whole screenshot was skipped. Capture rows and classification are unaffected |
| DB was auto-recovered | a `db_recovered` event exists | Read the event, see below |

Note: a locked or inactive session still records window metadata but skips the screenshot (`session_locked`, `session_inactive`), and classifies as `idle`. A stale warning at night is expected.

## 3. Heartbeat

```bash
familymonctl heartbeat-once      # as kid; writes one event
```

| Status | Age of last capture | Exit |
|---|---|---|
| `ok` | under 180 s | 0 |
| `stale` | 180 s to under 600 s | 1 |
| `dead` | 600 s or more | 2 |
| `never` | no captures | 3 |

The 1-minute timer runs it. A non-zero exit marks the unit failed on purpose. A gap in `heartbeat` events means the watcher itself was down. The watcher is a user timer today, so the kid can stop it.

## 4. Service and timer status

User scope, as `kid`:

```bash
systemctl --user status familymon-capture.service
systemctl --user list-timers 'familymon-*'
systemctl --user show familymon-capture.service -p ActiveEnterTimestamp -p ActiveState
journalctl --user -u familymon-capture.service --since '-30min'
```

System scope, root:

```bash
systemctl list-timers 'familymon-*'
systemctl status familymon-enforce.service familymon-webblock.service familymon-chargeback.service
journalctl -u familymon-enforce.service --since '-2h'
```

Stale code after upgrade: long-running user units (capture, dashboard) can keep old code. The deb postinst restarts them for logged-in users. If behaviour looks stale, compare the `ActiveEnterTimestamp` above with the package install time (for example from `/var/log/dpkg.log`). Restart as `kid`: `systemctl --user restart familymon-capture.service`.

Interpreting units:
- `familymon-enforce.service` exit 3 and "failed" means the apply guard refused, not that it crashed. See section 6.
- The user timers use `OnActiveSec`, so they arm at login. A capture gap after an unclean shutdown self-heals on login. Rows classified late appear as `unknown` until `familymonctl classify` runs. After a classification policy change use `familymonctl classify --redo` (it deletes only heuristic, domain and game_process labels, ignores `--limit`, and recomputes all history).

## 5. Events in the DB

Event kinds the code writes: `heartbeat`, `db_recovered` (user DB); `playtime_chargeback`, `grant_hours`, `grant_playtime`, `web_block`, `lesson_close` (root audit DB). `enforce apply` and `enforce revoke` write no event.

Read-only query. As `kid` for the user DB, root for the audit DB:

```bash
sqlite3 -readonly ~kid/.local/state/familymon/captures.db \
  "select at, kind, data from event order by id desc limit 20;"
sudo sqlite3 -readonly /var/lib/familymon/captures.db \
  "select at, kind, data from event order by id desc limit 20;"
```

Root-side jobs open the kid's DB read-only (SQLite `mode=ro`). Do the same when you inspect it. Do not write to either DB by hand. Reading the kid's DB as root exposes their activity: do it only when needed.

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
- Prerequisite: `web_block.enabled: true` and a schedule for the policy user (`schedule show` as `kid`). A typo in a top-level key of `policy.yaml` is silently ignored and leaves web blocking disabled, so confirm the dry run shows the config you expect.

### Chargeback

```bash
sudo familymonctl enforce chargeback --dry-run
```

It reports uncharged browser-game captures and today's charged total. The real run is capped at 1800 s per tick and writes a `playtime_chargeback` event.

### Schedule and conflicts

```bash
familymonctl schedule show        # as kid
familymonctl conflicts            # optionally --day YYYY-MM-DD
```

`schedule show` and `conflicts` print a "no schedule" message with exit 0 when none exists. That is not a success signal. After editing `/etc/familymon/schedule_extras.yaml`, run `familymonctl schedule refresh` as `kid`, since edits alone are not picked up. Quote every `HH:MM`.

### Classification and capture YAML (apps, games, messaging, domains, brands)

- `domains_starter.yaml`, `title_brands.yaml`: the package does not install these under `/etc/familymon/`; the bundled copies are used unless you place an override there. Overrides are read on each `classify` run (both files) and each web-block tick (`domains_starter.yaml` only). Run `classify --redo` for history, then compare `health` category counts.
- `messaging_apps.yaml`, `game_apps.yaml`: read once when `capture-run` starts (the code scans games at startup and builds the messaging list when the backend is created). Restart `familymon-capture.service` as `kid` to apply.
- Policy change versus bug: before changing a category rule, list the affected captures, show the parent examples, and state which rule matched (the `evidence_text` column). Do not edit rules from one data point.

### Notifications

`schedule notify-tick --test` sends one real popup (warn the kid). Confirm through Plasma's notification history or D-Bus state, never a screenshot. Block-start popups fire once per day per block (`notified.json`). Warnings repeat at most every 5 minutes per lesson and category (`warned.json`).

### Dashboard

```bash
systemctl --user is-active familymon-dashboard.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/            # expect 401
curl -s -o /dev/null -w '%{http_code}\n' -u 'parent:PASSWORD' http://127.0.0.1:8765/   # expect 200
```

A startup failure usually means `FAMILYMON_DASH_USER` or `FAMILYMON_DASH_PASS` is missing. The env file is read only at start, so restart after changes.

### Package upgrade

Check `dpkg -s familymon`, then user unit `ActiveEnterTimestamp` against the install time (section 4), then `health`.

## 7. Checklist for every change

Before:
1. `sudo -u kid -H familymonctl health`: exit 0, no unexplained warnings. Record the numbers. Do not apply updates onto an unhealthy capture pipeline.
2. Status of capture and timers (section 4). Capture must be running continuously.
3. Is the kid logged in? (`loginctl list-sessions`.) If yes, expect the apply guard to matter and avoid anything that narrows today's window.
4. State the change as BUG fix or POLICY choice. For policy, get the parent's decision first.
5. Preview: `enforce plan`, `enforce web-block --dry-run`, `enforce chargeback --dry-run`. Commands without a preview (`schedule import`, `schedule refresh`, `enforce grant`, `enforce revoke`): save a copy of the current file or output first (`schedule show`, `enforce show kid`).
6. If code or tests are involved: run the test suite alone and read its exit code. Build and ship in separate calls.

After:
1. Read back (section 6) after waiting about 5 s for timekpr.
2. Confirm the matching event exists, if the command writes one (section 5).
3. `sudo -u kid -H familymonctl health` again: still exit 0, capture not stale, no new `db_recovered`, classification percentage not dropped.
4. Check `systemctl list-timers 'familymon-*'` and failed units: `systemctl --failed` and `systemctl --user --failed` (as `kid`).
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
| Enabling a timer | `systemctl disable --now <timer>` |
