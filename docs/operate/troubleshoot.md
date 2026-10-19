# Troubleshoot

Read this when something looks wrong on a running familymon install: stale behaviour after an upgrade, captures labelled `unknown`, missing notifications, PlayTime not counting, a grant that vanished, the kid logged out unexpectedly, a blocked site still open, a stale/DOWN heartbeat, or `capture_failed` / `capture_tamper` events. Each entry is symptom, diagnosis (read-only commands first), fix. For routine checks see [verify-and-health.md](verify-and-health.md). For command flags see [../reference/cli.md](../reference/cli.md). For paths and units see [../reference/files-and-units.md](../reference/files-and-units.md). For concepts see [../concepts.md](../concepts.md).

## Ground rules

- **Safety: never take a raw screenshot of the kid's session for diagnostics.** It bypasses messaging-window redaction. Verify notifications through the desktop's notification history, D-Bus state, or familymon's own redacting capture.
- **Parent decides policy.** Establish facts first. Separate a BUG (code did not do what the rule says) from a POLICY question (the rule itself). Fix bugs. Present policy trade-offs to the parent with examples. Never change classification or enforcement policy on one ambiguous data point.
- Diagnose with read-only commands first. `familymonctl health` and `conflicts` open the DB read-write, and opening can trigger self-recovery (see [DB was auto-recovered](#db-was-auto-recovered)), so they are not strictly side-effect free. `familymonctl events` is read-only.
- Everything runs as root and uses one state dir, `/var/lib/familymon`, including the capture DB `/var/lib/familymon/captures.db`. Run every familymonctl command with `sudo`. A non-root run resolves a separate dev state dir (`~/.local/state/familymon`) and tells you nothing about production. Every unit is a system unit: use `systemctl`, `journalctl -u`, never `--user`.
- Tests and dev runs must never touch real processes or real user state. Reproduce destructive paths only against processes you spawned.

## Quick triage

| Symptom | Go to |
|---|---|
| Behaviour looks like old code after an upgrade | [Stale code after upgrade](#stale-code-after-upgrade) |
| Heartbeat `stale` / `dead` / `never`, dashboard says "Capture stale" or "Capture DOWN" | [Heartbeat stale or DOWN](#heartbeat-stale-or-down) |
| Dashboard says "Capture DOWN while the session is active" | [Capture DOWN while the session is active](#capture-down-while-the-session-is-active) |
| Dashboard shows a red "capture(s) failed ... forged window-list attempt(s)" line, or `events` lists `capture_failed` / `capture_tamper` | [capture_failed and capture_tamper events](#capture_failed-and-capture_tamper-events) |
| Dashboard says "Heartbeat watcher not running" | [Heartbeat watcher not running](#heartbeat-watcher-not-running) |
| Rows show `unknown` | [Captures shown as unknown](#captures-shown-as-unknown) |
| Timers listed but never fire after late login | [Timers not armed](#timers-not-armed) |
| `health` prints "DB was auto-recovered" or exits 4 | [DB was auto-recovered](#db-was-auto-recovered) |
| Familymon notifications do not pop up | [Notifications not popping up](#notifications-not-popping-up) |
| A game is not drawing down PlayTime | [PlayTime not counting a game](#playtime-not-counting-a-game) |
| `timekpra --getuserinfo` shows enabled flags as False | [Booleans misreported by timekpra](#booleans-misreported-by-timekpra) |
| An extension of allowed hours disappeared | [Grant reverted](#grant-reverted) |
| `enforce apply` exited 3, or the kid was logged out | [Kicked off by an hours change](#kid-kicked-off-by-an-hours-change) |
| A blocked site is still open in a tab | [Blocked site still open in a tab](#blocked-site-still-open-in-a-tab) |
| Chrome is running but the extension is not | [Chrome without the extension](#chrome-without-the-extension) |
| The kid used another browser in a lesson | [Another browser used in a lesson](#another-browser-used-in-a-lesson) |
| Dashboard does not load | [Dashboard unreachable](#dashboard-unreachable) |

## Stale code after upgrade

**Symptom.** Behaviour matches the previous version after a package upgrade. Long-running units (capture supervisor, dashboard) keep old code in memory.

**Diagnose (read-only).** Compare when the unit last started with when the package was installed.

```bash
systemctl show familymon-capture.service -p ActiveEnterTimestamp
systemctl show familymon-dashboard.service -p ActiveEnterTimestamp
# package install time
dpkg-query -W -f='${Version}\n' familymon
ls -l --time=birth /opt/familymon/venv 2>/dev/null || ls -ld /opt/familymon/venv
```

If `ActiveEnterTimestamp` is older than the install, the unit runs old code.

**Why.** The deb's postinst runs `daemon-reload`, enables the units and restarts `familymon-capture.service` and `familymon-dashboard.service`, with errors suppressed. It always exits 0. A unit skipped by its `ConditionPathExists` (no `policy.yaml`, no `dashboard.env`) is not running, so it is not stale. Timer-driven oneshots (classify, heartbeat, notify, enforce, chargeback, webblock) pick up new code on their next run.

**Fix.** Restart the stale unit, root.

```bash
sudo systemctl restart familymon-capture.service
sudo systemctl restart familymon-dashboard.service
```

**Safety.** Restarting the capture supervisor re-scans the Steam library and leaves a short capture gap, which `health` may report as a gap over 300 s only if it lasts that long.

**Undo.** Not applicable.

## Heartbeat stale or DOWN

**Symptom.** `sudo familymonctl heartbeat-once` exits non-zero, `familymon-heartbeat.service` shows failed, or the dashboard banner says "Capture stale" or "Capture DOWN".

| Status | Meaning | Exit code | Banner |
|---|---|---|---|
| `ok` | newest capture under 180 s old | 0 | "Capture: OK" |
| `idle` | the kid has no graphical session and nothing fresh | 0 | "Not logged in" |
| `stale` | 180 s to under 600 s | 1 | "Capture stale" |
| `dead` | 600 s or more | 2 | "Capture DOWN" (with "while the session is active" etc. when the session is known) |
| `never` | no captures at all | 3 | "No captures yet" |
| `watcher_down` (dashboard only) | newest heartbeat event older than 5 min | n/a | "Heartbeat watcher not running" |

**Diagnose (read-only).**

```bash
sudo familymonctl health                       # last capture age, largest gap 24h
systemctl status familymon-capture.service
journalctl -u familymon-capture.service -n 50 --no-pager
systemctl list-timers 'familymon-*'
sudo familymonctl events --kind heartbeat      # the recorded status and session per minute
```

Check these in order:

1. Is the kid logged in at all? `idle` / "Not logged in" is normal overnight: no session, nothing to capture. The supervisor skips the tick and records nothing. (`loginctl list-sessions`.)
2. Is the capture service active? `systemctl status familymon-capture.service`. If it shows "inactive (dead)" with a failed condition, `/var/lib/familymon/policy.yaml` does not exist: create it and `sudo systemctl restart familymon-capture.service`.
3. Is the kid's graphical session active and unlocked? Locked or inactive sessions still record window metadata but skip screenshots (`session_locked` / `session_inactive`), so captures continue.
4. Did the laptop sleep? `health` warns "`>5 min gap exists in the last 24h` (crash, or laptop sleep)".
5. Is capture failing every tick while the session is open? See [capture_failed and capture_tamper events](#capture_failed-and-capture_tamper-events).

**Fix.** Start or restart the capture service. If it restarts and fails, read the journal. The unit has `Restart=always`, `RestartSec=10`.

```bash
sudo systemctl restart familymon-capture.service
```

## Capture DOWN while the session is active

**Symptom.** The dashboard says "Capture DOWN while the session is active" (or "Capture stale ... session is active"). The heartbeat is `dead` or `stale` and its `session` field is `active`. The kid is logged in and capture is not landing.

**Diagnose (read-only).** The kid cannot stop the supervisor (root system unit), so either it is broken or the kid is interfering with ticks.

```bash
systemctl status familymon-capture.service
journalctl -u familymon-capture.service -n 80 --no-pager
sudo familymonctl events --kind capture_failed --kind capture_tamper
loginctl list-sessions
```

1. Service not running or restarting: read the journal for the reason (missing `policy.yaml` or monitored user, crash). Fix and restart it.
2. Service running, `capture_failed` events every minute: the probe cannot get the KWin window list from the kid's session. Read the `error` field (see the next section).
3. A text-console login or a session that has no KWin (`session` not `active`, or a Wayland socket missing): nothing to capture, and the supervisor reports the session state, not a fault.
4. Neither events nor captures: the heartbeat timer or supervisor loop is wedged. `sudo systemctl restart familymon-capture.service`.

**Parent decides** what to do about a kid who is deliberately breaking capture. Report the events (times, counts) rather than speculating; a log-out, a KWin restart or a crash of the compositor also produces them.

## capture_failed and capture_tamper events

**Symptom.** The dashboard shows a red line such as "3 capture(s) failed while the session was open; 1 forged window-list attempt(s) rejected", or `sudo familymonctl events` lists these kinds.

```bash
sudo familymonctl events --day 2026-10-19 --kind capture_failed --kind capture_tamper
```

| Event | Data | Meaning |
|---|---|---|
| `capture_failed` | `error`, `session` | A tick ran while the kid had a session, but the probe failed or the KWin window list never arrived. No capture row and no screenshot were written for that tick |
| `capture_tamper` | `sink_rejected`, `session` | A window-list push to the sink was rejected: it came from the wrong D-Bus sender or carried the wrong nonce. Something other than KWin's own script tried to deliver a window list |

Common `capture_failed` errors:

| `error` | Likely cause |
|---|---|
| `no session bus` | The session ended between the logind check and the probe: log-out, usually harmless |
| `probe timed out after 45s` | KWin or the session was hung, or the probe was killed and restarted slowly |
| `probe exited N: ...` | The probe crashed or was killed. The last lines of its stderr follow; a repeated `Killed` or exit 137 at the same cadence suggests something is killing it |
| `could not start the probe: ...` | `setpriv` failed (user removed, or `setpriv` missing: it comes with util-linux) |
| `KWin window list unavailable` | KWin did not deliver the list: KWin restarting, the sink name held by another process, or an interfered script |

**Diagnose.** Establish facts first. A few scattered `capture_failed` events around log-out, suspend/resume, a KWin crash or a Plasma restart are noise. A sustained run (every minute for a long stretch while the session is `active`), or any `capture_tamper`, is worth the parent's attention. Check the timestamps against `loginctl` session history and `journalctl -b -u familymon-capture.service`.

**Fix.** There is nothing to repair for scattered events. For a persistent run: confirm KWin is running in the kid's session, then `sudo systemctl restart familymon-capture.service`. For `capture_tamper`: nothing prevents a same-user process from trying, and the sink rejected it, so no forged data was stored. Tell the parent; **the parent decides** how to respond. Never relax monitoring or add an exception because the kid asks. See [../develop/tamper-resistance.md](../develop/tamper-resistance.md).

## Heartbeat watcher not running

**Symptom.** The dashboard banner says "Heartbeat watcher not running": the newest `heartbeat` event is more than 5 minutes old.

**Diagnose (read-only).**

```bash
systemctl list-timers 'familymon-heartbeat.*'
systemctl status familymon-heartbeat.service
journalctl -u familymon-heartbeat.service -n 30 --no-pager
sudo familymonctl events --kind heartbeat | tail -5
```

If the machine was just off or asleep, wait a few minutes. Otherwise the timer is disabled or the service is failing (a non-zero exit is expected for stale/dead and does not stop the timer). The kid cannot disable it, so this means a service problem or someone with root.

**Fix.** `sudo systemctl enable --now familymon-heartbeat.timer`.

## Captures shown as unknown

**Symptom.** Timeline rows, `health` category counts, or conflicts show `unknown`. `health` may show a low `classified` percentage.

**Diagnose (read-only).** Distinguish three cases.

1. **Not yet classified.** Rows without a classification show as `unknown` on the dashboard. The classify timer runs every 5 minutes (first run 2 min after boot). Check:

   ```bash
   sudo familymonctl health          # "classified 24h" percentage
   systemctl list-timers familymon-classify.timer
   systemctl status familymon-classify.service
   ```

2. **Timer never armed** after a late login. See [Timers not armed](#timers-not-armed).
3. **Classified as `unknown` on purpose.** The heuristic found no signal (evidence "no signal from any visible window", confidence 0.1), or the classifier raised an error on that row (evidence starts `classifier error:`, confidence 0.0). Apps that exist only in user space (for example under `~/.local/share/applications`) classify as `unknown` because per-user `.desktop` files are forgeable and deliberately ignored.

**Fix.**

- Case 1 and 2: run classify as root. It is idempotent and only labels rows without a classification.

  ```bash
  sudo familymonctl classify
  ```

- To relabel history after a classification rule or list change:

  ```bash
  sudo familymonctl classify --redo
  ```

  `--redo` deletes classifications whose source is `heuristic`, `domain` or `game_process` and recomputes them. Other sources are kept. `--limit` is ignored with `--redo`. Relabelling rewrites history, so tell the parent first.

- Case 3: this is policy, not a bug. **Parent decides** whether to add a domain, brand or app mapping. Do not change rules from a single ambiguous sample. Show the parent the evidence text and the candidates, then edit the relevant config file (see [../reference/config.md](../reference/config.md)) and run `classify --redo`.

**Undo.** `classify --redo` is not reversible except by restoring the DB. Back up `captures.db` first if unsure.

## Timers not armed

**Symptom.** A timer exists but never fires. Captures stay unclassified, no heartbeat events appear, no notifications.

**Background.** Every timer is a system timer now, armed by the system manager at boot, so the old failure (a user timer that never armed when the kid logged in long after boot) cannot happen. A timer that does not fire is disabled, was removed by hand, or its service is skipped by a `ConditionPathExists` (no `/var/lib/familymon/policy.yaml`).

**Diagnose (read-only).**

```bash
systemctl list-timers --all 'familymon-*'     # NEXT column must not be empty
systemctl status familymon-classify.timer familymon-heartbeat.timer familymon-notify.timer
systemctl cat familymon-classify.timer        # check for local drop-ins
```

**Fix.**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now familymon-classify.timer familymon-heartbeat.timer familymon-notify.timer
sudo familymonctl classify        # catch up immediately
```

The package enables these on every install and upgrade, so a disabled timer means someone with root disabled it. Reinstalling the package (or running the `enable` above) restores it.

## DB was auto-recovered

**Symptom.** `familymonctl health` prints `! DB was auto-recovered: ...`, or exits 4 with "DB INTEGRITY: ... (automatic recovery failed)".

**How it works.** Every DB open (capture, classify, heartbeat, health, dashboard) runs `PRAGMA quick_check(5)` first. A "locked" or "busy" result counts as ok. On damage familymon:

1. Renames the file to `captures.db.corrupt-<YYYYmmdd-HHMMSS>`, together with its `-journal`, `-wal` and `-shm` siblings. The damaged file is never deleted.
2. Tries, in order: `sqlite3 <damaged> .recover` piped into a fresh DB (needs the `sqlite3` binary), Python `iterdump` (best effort, stops at the first unreadable page), then a fresh empty DB.
3. Records an event of kind `db_recovered` with `method`, `finding`, `damaged_backup` and `salvaged_tables`.

Monitoring continues in every case.

**Diagnose (read-only).**

```bash
sudo familymonctl health                      # shows method, backup path, salvaged tables
sudo ls -l /var/lib/familymon/ | grep corrupt
```

Exit codes from `health`: 2 is a privacy violation (checked first), 4 is integrity not ok.

**Fix.**

- Exit 0 with the auto-recovered note: no action needed. Keep the `.corrupt-*` file until the parent is satisfied that the salvaged data is adequate. Nothing purges it.
- Exit 4: recovery failed to produce a healthy file. Check free disk space and that `sqlite3` is installed (it is a package dependency). Inspect the journal of the unit that opened the DB. Do not delete the `.corrupt-*` file.
- Exit 2 (privacy violation): a capture with a messaging window present has an un-redacted screenshot on disk. Treat as a bug. Stop and report to the parent. Do not open the screenshots to investigate. Identify affected rows by metadata only.

**Safety.** Recovery of rows depends on the damage. Salvaged tables are listed with row counts. The fresh-DB fallback loses history from the live file, though the damaged file remains on disk.

## Notifications not popping up

**Symptom.** Block-start notices, live lesson warnings, or "closed during lesson" notices do not appear.

**Diagnose (read-only).**

1. Desktop entry present? It sets the `desktop-entry` hint that lets Plasma treat familymon as a known app.

   ```bash
   ls -l /usr/share/applications/familymon.desktop     # NoDisplay=true
   ```

2. Is the notify timer armed, and is the kid logged in? The tick runs as root and delivers into the kid's session; with no session there is nothing to deliver to (`sudo familymonctl schedule notify-tick --test` prints "no session to notify").

   ```bash
   systemctl list-timers familymon-notify.timer
   journalctl -u familymon-notify.service -n 30 --no-pager
   ```

3. Does the schedule exist? `notify-tick` exits silently if there is no schedule file. `sudo familymonctl schedule show` prints it (read-only).
4. Is the block flagged `notify`? Only `notify: true` blocks get start notifications. Each fires at most once per day (`notified.json`). Live warnings fire only during a `school` block, once per cooldown (default 5 minutes) per lesson and category (`warned.json`).
5. Is Plasma configured to show popups only for apps with a desktop entry, or is Do Not Disturb on? Did the kid turn familymon's notifications off in Plasma settings? Notifications are advisory and the kid can mute them; monitoring and enforcement are unaffected. Check the Plasma notification history rather than taking a screenshot of the session.
6. All notices (block start, warnings, lesson close) are delivered with `runuser` on the user's bus at `/run/user/<uid>/bus`. If the bus is missing the notice is not delivered. A block-start notice or warning is marked done (`notified.json`, `warned.json`) only when it was delivered, so it is retried on the next tick.

**Fix.**

- Send a sample into the kid's session (warn them first), as root:

  ```bash
  sudo familymonctl schedule notify-tick --test
  ```

  This sends "Schedule notifications are working." and exits.
- If the sample appears but scheduled ones do not, the cause is schedule or watermark state, not delivery. Watermarks reset daily.
- If the sample does not appear, reinstall the package (restores the `.desktop` file), then check Plasma notification settings.

**Safety.** Do not screenshot the session to verify.

## PlayTime not counting a game

**Symptom.** The kid plays a game and PlayTime left does not decrease, or per-user PlayTime settings seem ignored.

**Diagnose (read-only).** Run as root or a `timekpr` group member.

```bash
familymonctl enforce show kid         # playtime enabled? activity masks listed?
familymonctl enforce plan                # do live settings match policy.yaml?
grep -n 'PLAYTIME' /etc/timekpr/timekpr.conf
```

Check, in order:

1. **Global switches.** PlayTime is inert until the GLOBAL switches in `/etc/timekpr/timekpr.conf` are on: `TIMEKPR_PLAYTIME_ENABLED` and `TIMEKPR_PLAYTIME_ENHANCED_ACTIVITY_MONITOR_ENABLED`. Until then, per-user enable is silently masked. Familymon never reads or writes `timekpr.conf`, so this is a manual prerequisite.
2. **`games.enabled: true`** in `policy.yaml`. When games are disabled, familymon writes PlayTime limits of 0 and no activities.
3. **Masks.** timekpr PlayTime masks are effectively full-match against the process path and cmdline. Masks must look like `.*something.*`. Familymon wraps bare `games.extra_masks` entries as `.*mask.*`. Steam game masks are generated from installed `appmanifest_*.acf` entries as `.*common.<installdir>.*`, covering native and Proton games.
4. **Flatpak apps** appear as their `bwrap` wrapper, so a mask on the app name may not match. Use a mask that matches the wrapper's command line, and verify against a real process listing. The bundled `policy_example.yaml` shows this shape for a Flatpak Roblox client: `.*bwrap.*sober.*[Roblox]` (the bracketed part is the activity description).
5. **Poll delay.** timekpr applies a play-time-left change on its next poll, about 3 s (timekpr-next behaviour, observed with 0.5.10; not defined in familymon's code). Read back after a few seconds.
6. **Browser games** are invisible to timekpr. They are billed by `enforce chargeback`, which needs `games.enabled` and `games.charge_browser_games` in `policy.yaml`. Preview with:

   ```bash
   familymonctl enforce chargeback --dry-run
   ```

   Chargeback only bills today's captures classified `games` where `game_running` is false and the active window looks like a browser, up to 1800 s per run (default cap). `games.charge_browser_games_when` is `visible` (default, a game visible anywhere bills the minute) or `focused` (the focused window's own title must be a game). Any other value is an error. It depends on classify having run (see [Captures shown as unknown](#captures-shown-as-unknown)).

**Fix.**

- Flip the global switches by hand in `/etc/timekpr/timekpr.conf`, then restart timekpr. The exact restart procedure is not in familymon's code. Confirm it against the timekpr-next documentation for your version.
- Fix masks or policy in `policy.yaml`, then preview and apply:

  ```bash
  familymonctl enforce plan
  familymonctl enforce apply
  ```

  `apply` writes only the delta. It refuses with exit 3 if the change would cut off an active session (see [Kicked off by an hours change](#kid-kicked-off-by-an-hours-change)).

**Parent decides** which games and masks count as play time.

## Booleans misreported by timekpra

**Symptom.** `timekpra --getuserinfo kid` prints every enabled boolean as `False`.

**Cause.** A CLI display bug in timekpr 0.5.10. The setting itself is fine.

**Diagnose / fix.** Do not trust the CLI for reads. Read live state over D-Bus through familymon:

```bash
familymonctl enforce show kid
familymonctl enforce plan
```

Familymon reads via D-Bus and writes via `timekpra`. Do not "fix" a setting because the CLI shows False.

## Grant reverted

**Symptom.** An extension of allowed hours was set and later disappeared, or an hours change was reverted within the hour.

**Cause.** The hourly `familymon-enforce.timer` reconciles live timekpr state back to `policy.yaml`. A raw `timekpra --setallowedhours` is persistent and not recorded, so the reconciler reverts it. Date-bound grants are folded into the desired state and expire on their own.

**Diagnose (read-only).**

```bash
familymonctl enforce grants        # active grants (all users)
familymonctl enforce plan          # what the reconciler would change
```

Note `enforce plan` may prune expired grants from `grants.json` as a side effect. A grant is active when `day == today` and now is at or before its `end`. A grant made for yesterday, or one past its end time, is gone. Grants only widen today's allowed hours.

**Fix.** Use a grant, never a raw `timekpra` call. Needs root.

```bash
familymonctl enforce grant kid --until 22:30 --note "reason"
familymonctl enforce grant kid --playtime 20m
```

At least one of `--until` or `--playtime` is required. `--from HH:MM` sets the start (default now). `--until` is inclusive and must not be earlier than the start (`--from`, default now). `--playtime` adjusts the PlayTime counter directly and is not stored, so it cannot be "reverted" but is also not listed by `enforce grants`.

**Undo.** `familymonctl enforce revoke kid` drops today's hour grants and converges to policy. It runs the safety guard and has no `--force`, so it may skip narrowing while the kid is logged in. The reconciler converges once safe. The grants are deleted either way.

**Safety.** `enforce grant` stores the grant before checking that the policy user matches. A mismatch errors but leaves the grant stored.

## Kid kicked off by an hours change

**Symptom.** The kid was logged out or locked mid-session, or `enforce apply` fails with exit 3 ("refusing to apply - it would cut off an active session").

**Cause.** Allowed-hours changes take effect immediately. If the new window excludes the current hour, timekpr applies the lockout type. The apply guard refuses when a user session exists and either no desired window covers now, or today's desired daily limit is lower than today's live limit. The limit check is a conservative approximation and does not track time already spent. The guard counts all logind sessions for the user and assumes one session if `loginctl` fails.

**Diagnose (read-only).**

```bash
familymonctl enforce plan      # prints "applying NOW would be unsafe" and reasons; exit 0
familymonctl enforce show kid  # live lockout type, hours, limits
```

**Fix.**

- Prefer waiting for logout. The hourly timer retries after exit 3, and the unit shows failed until then.
- To widen hours for tonight, use `enforce grant` (no guard needed, since grants only widen).
- `familymonctl enforce apply --force` overrides the guard. **Parent decides.** **Safety:** this can end the kid's session immediately. Use only with the parent's explicit approval.
- Hour windows are inclusive whole hours: `{start: 7, end: 21}` allows 07:00 to 21:59.
- Hour changes are applied last in the change list because narrowing is the riskiest change.

**Undo.** Restore the previous values in `policy.yaml` and run `enforce plan` then `enforce apply`, or add a grant for today.

## Blocked site still open in a tab

**Symptom.** During a lesson a distraction site is still showing in an already-open tab.

**Cause.** Chrome's `URLBlocklist` policy only applies on navigation. Already-open tabs are handled by the force-installed extension, which redirects matching tabs to its own `blocked.html` page (it navigates the tab and does not close it). The extension sweeps on install, startup, managed-policy change, tab URL change, and once per minute.

**Diagnose (read-only).** Run as root, or as the kid for the file checks.

```bash
familymonctl enforce web-block --dry-run     # desired policy, block set, 'change needed'
cat /etc/opt/chrome/policies/managed/familymon.json
familymonctl schedule show                   # is there a school block now? any break today?
```

In Chrome, `chrome://policy` shows loaded policies. Do not screenshot the kid's session.

Check:

1. A `school` block must be in progress now. Outside lessons the document has no `URLBlocklist` and the extension is `active: false`. Schedule breaks suspend blocks.
2. `web_block.enabled: true` in `policy.yaml`. Without it the policy file is removed.
3. The domain is in a blocked category (default `messaging`, `social`, `games`), or in `extra_domains`, and not in `allow_domains`. `video` is not blocked by default. Streaming is flagged by live warnings, not blocked.
4. `meet.google.com`, `teams.microsoft.com` and `zoom.us` are never blocked.
5. Is the schedule imported? If the policy user's `schedule.yaml` is missing, there is nothing to block.
6. Is `familymon-webblock.timer` active? The package enables it; it ticks every minute. `systemctl is-active familymon-webblock.timer`.
7. Is the extension present and running? See next entry. Incognito or guest windows are covered by `block_private`, not by the extension.

**Fix.** Re-enable the timer if someone with root disabled it, and run one tick (root):

```bash
systemctl enable --now familymon-webblock.timer
familymonctl enforce web-block        # one tick now; needs root
```

A tab on a domain missing from the list is a policy question. **Parent decides** whether to add it to `extra_domains`.

## Chrome without the extension

**Symptom.** URL blocking applies to new navigations but open tabs stay, or `chrome://extensions` lacks "familymon lesson block".

**Diagnose (read-only).**

```bash
ls -l /usr/share/familymon/chrome-extension/      # familymon.crx update.xml id version
familymonctl enforce web-block --dry-run          # prints the extension description
ps -o args= -u kid | grep -i -- --disable-extensions
```

- If the directory is incomplete, the dry run says the extension is "not packaged" and no force-install entry is written.
- Chrome started with `--disable-extensions` runs without the extension. During a lesson the sweep closes such a Chrome (rule "chrome (extensions disabled)").
- Chrome fetches the extension through `file://` URLs from `update.xml`. A new build must have a higher version or Chrome keeps the old one. A lost signing key only changes the extension id.
- The extension requires Chrome 120 or later.

**Fix.** Reinstall the package so the extension files are present, then run a web-block tick (root). If a Chrome with `--disable-extensions` is open during a lesson, the next tick closes it (SIGTERM, then SIGKILL after 3 s) and notifies the kid.

**Safety.** The sweep kills processes of the policy user. It refuses uid 0 and requires root. Never run it on a dev machine against your own user.

## Another browser used in a lesson

**Symptom.** The kid opens Firefox, Brave, or similar during a lesson, or you want to confirm what would be closed.

**Behaviour.** With `web_block.forbid_other_browsers` (default true), non-Chrome browsers, matched by exe path component or Flatpak id, are closed during a lesson. Matching is by path component equal to the name, or starting with `name-`, `name.` or `name_`. It is not substring, so "zenity" is not "zen". Only Chrome is governed by policy. `forbid_apps` masks (full-match, case-insensitive, against `"<exe> <cmdline>"`) extend the list. Closing happens only while a school block is in progress.

**Diagnose (read-only).**

```bash
familymonctl enforce web-block --dry-run     # "forbidden apps running now (closed only during a lesson)"
```

The dry run scans processes read-only and kills nothing.

**Check.**

- Not closed outside a lesson: expected.
- Not closed during a lesson: is `web_block.enabled` true, `forbid_other_browsers` true, and `familymon-webblock.timer` active (`systemctl status familymon-webblock.timer`)? Is the browser in the built-in list? If not, add a `forbid_apps` mask. **Parent decides.**
- Look for a `lesson_close` event in the root audit DB (`/var/lib/familymon/captures.db`). It records user, subject, apps, pids and which needed SIGKILL.
- If the kid says they received no notice, see [Notifications not popping up](#notifications-not-popping-up). A missing bus at `/run/user/<uid>/bus` makes the notice silent.

**Fix.** Tick manually as root with `familymonctl enforce web-block`. A dry run never kills anything. A non-root real run fails with "cannot write ... run as root" when the policy file needs changing, or with "closing processes needs root" when it would close processes.

## Dashboard unreachable

**Symptom.** The browser cannot reach the dashboard, gets 401, or the service restarts in a loop.

**Diagnose (read-only).**

```bash
systemctl status familymon-dashboard.service
journalctl -u familymon-dashboard.service -n 30 --no-pager
ls -l /etc/familymon/dashboard.env
ss -ltn | grep 8765
```

Check:

1. **Credentials.** The app raises `RuntimeError` at startup if `FAMILYMON_DASH_USER` or `FAMILYMON_DASH_PASS` is unset or empty. The unit reads them from `/etc/familymon/dashboard.env` (not shipped; systemd reads it as root before dropping to the `familymon` user). Create it with both variables, owner root:root, mode 0600. If the file does not exist the unit is skipped by `ConditionPathExists` and shows as inactive, not failed.
2. **Not started.** The package enables the unit, but it only starts once the env file exists: `sudo systemctl restart familymon-dashboard.service`.
3. **Bind address.** Default is `127.0.0.1`, reachable only from the same machine. Set `FAMILYMON_DASH_BIND` (for example `0.0.0.0`) for LAN access, or pass `--bind`. Port is `FAMILYMON_DASH_PORT`, default 8765.
4. **401.** HTTP Basic, realm `familymon`, one shared user and password. All routes require it.
5. **No TLS.** The server serves plain HTTP. **Safety:** exposing it beyond localhost sends credentials and the kid's activity metadata unencrypted. The dashboard shows metadata only. No screenshot route exists.
6. **Wrong data or no data.** The dashboard runs as `familymon` with `FAMILYMON_STATE_DIR=/var/lib/familymon` and opens the DB read-only through group read access. If it shows nothing, check `ls -ld /var/lib/familymon` (root:`familymon` 0750) and that `captures.db` is group-readable (`sudo ls -l /var/lib/familymon/captures.db`).
7. **Stale code** after upgrade: see [Stale code after upgrade](#stale-code-after-upgrade).

`/d/<day>` with an invalid date returns 400. `/api/captures` and `/api/stats` do not catch the parse error for an invalid `day`, so they should return a server error (500). This is read from the code, not exercised.

**Fix.** Create or correct `dashboard.env`, then:

```bash
sudo systemctl restart familymon-dashboard.service
```

## Unsure claims

Items in this file that are not fully pinned down by the code, so verify before relying on them:

- Exact procedure to make timekpr re-read `/etc/timekpr/timekpr.conf` (not in familymon code).
- Whether other Flatpak games need a different pattern. Only the `.*bwrap.*sober.*[Roblox]` example and "appears as the bwrap wrapper" are in the code.
- How long timekpr takes to apply a play-time-left change (not defined in familymon code).
- Whether probe failures with `probe exited N` have a single cause: the message is the tail of the probe's stderr and varies.
