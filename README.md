# familymon

Local-first parental-control + digital-wellbeing monitoring for Linux laptops.

Captures screenshots and window state every ~60 s, classifies activity using cheap local heuristics plus a small local vision model, and surfaces the result against a school schedule so a parent can see what actually happened — and, later, enforce blocking rules.

**Privacy is the design constraint, not an afterthought.** Screenshots stay on the laptop they were captured on. Messaging-app windows are skipped at capture time. Classification runs locally on a small VLM after a bootstrap labeling phase that uses an interactive Claude Code session, not the Anthropic API.

The full design lives in [`docs/PLAN.md`](docs/PLAN.md).

## Status

**Phase 1 complete** — the capture pipeline is live and dogfooding on the dev machine:

- KWin Wayland capture backend (Plasma 6): window enumeration via KWin scripting
  D-Bus, screenshots via a Spectacle subprocess (KWin's own ScreenShot2 interface
  is caller-authorized and rejects us).
- 60 s capture loop with perceptual-hash deduplication (a screenshot is saved
  only when visually novel — so a static screen collapses to one, but a playing
  video, which keeps one window title while its content changes, is still
  captured frame-by-frame).
- Messaging-app privacy redaction: every visible chat window's rectangle is
  blacked out of the screenshot (located from the window manager's geometry,
  never by analysing pixels) — the rest of the screen is kept. Works across
  multi-monitor incl. mixed-DPI (spectacle composites at a uniform scale; a
  uniform-scale guard falls back to dropping the screenshot if that ever
  fails). Native messengers are matched by window class; web chats (e.g.
  WhatsApp Web) by a strict, anchored window-title pattern that redacts the
  whole browser window. Background/unfocused chats are redacted too.
- Heuristic classifier: app categories from a `.desktop` scan, domain categories
  from a bundled list, and brand-keyword matching on browser titles
  (YouTube→video, Reddit→social, …) as a stopgap until real URL extraction.
- Multi-window classification: each capture is categorised from **all** visible
  windows, not just the focused one — a focused chat can't mask a visible game
  or video (see "Classification model" below).
- logind session detection: captures while the screen is locked or the session
  is backgrounded are tagged and classified `idle`, with the screenshot skipped.
- Trusted game detection: a window whose process belongs to an installed game
  (Steam library scan — `appmanifest` install dirs across all libraries, tools
  excluded — plus a parent-curated list) classifies as `games`, regardless of
  focus. Deliberately *not* GameMode: that's a performance opt-in the monitored
  user could fake (`gamemoderun firefox`) to disable monitoring; detection keys
  on what's actually installed, which the teen can't trivially forge.
- LAN-only FastAPI dashboard (HTTP basic auth) with a per-day timeline.
- School schedule import: pull the weekly timetable (Kings InterHigh student API
  JSON) into a parent-editable recurring-weekly schedule via
  `familymonctl schedule import`; times are stored in the kid's local zone.
  Manual per-term re-import (no stored school credentials).
- Heartbeat watcher: an independent systemd timer (1/min) records capture
  liveness in the event log and the dashboard shows a status banner (OK /
  stale / DOWN / no captures yet) on every page. Catches the case where the
  capture loop has died — the timeline going quiet on its own is easy to miss
  or misread as "nobody used the laptop." In production this will run as a
  root service so it survives a teen stopping their own user units (phase 4).
- Schedule extras: a parent-maintained `/etc/familymon/schedule_extras.yaml`
  is merged on every import — subjects to exclude from monitored time
  (self-paced work) and custom blocks (self-study, workout, …). Blocks in any
  category other than `school` are never conflict-monitored; `notify: true`
  pops a desktop notification in the kid's session when the block starts
  (`familymon-notify.timer`, per minute, deduped per day). `breaks:` lists
  date ranges with no lessons (half term, Christmas): the lessons stay in
  the schedule but are out of effect for those dates, so web blocking, the
  lesson sweep, live warnings and the Conflicts panel pause and resume on
  their own (by default only `school` blocks pause; a break can list more
  categories to silence the self-study reminders too). `familymonctl
  schedule refresh` re-applies an edited extras file to the current schedule
  without a fresh timetable export.
- Conflict detection: flags distraction activity during a scheduled lesson.
  Always-distraction categories default to games/social/messaging.
  `browsing_other` is not flagged (online classes run in the browser). `video`
  is flagged *only* when it's a recognized entertainment streaming platform
  (YouTube, Netflix, …, matched by brand on the window title) — a generic class
  stream is left alone. Surfaced on the dashboard (a "Conflicts" panel + red
  timeline rows) and via `familymonctl conflicts`.
  *Planned inversion:* once a whitelist of legitimate class platforms (Adobe
  Connect & co.) exists, flip the video rule to flag any video during a lesson
  except whitelisted ones.
- `familymonctl health` — data-health report, including a hard privacy check.

### Classification model

Each capture has one **primary** category, chosen by a **priority rule** over
the visible windows (leisure/distraction — games, video, social — outrank
productive work, which outranks a messaging overlay). This closes the "focused
chat hides a background game" loophole and keeps the timeline simple.

Alongside the primary, each classification also records **`present_categories`**
— the full priority-ordered set of categories visible that tick. The dashboard
surfaces it as an "also: …" annotation (e.g. `video · also: messaging`), so the
multi-window reality is visible at a glance.

**Still deferred (the heavy half of hybrid):** overlap-aware *time* accounting —
letting one minute count toward several categories with stats that sum past
wall-clock ("a game was on screen for N of M school-hour minutes," independent
of focus). The present-set data is now being recorded, so that upgrade has
history to work with when built.

Run it: `familymonctl capture-run` (collector), `familymonctl classify` (heuristics),
`familymonctl dashboard` (viewer), `familymonctl health` (diagnostics).

**Phase 2 complete.** Logind session detection, multi-window classification,
per-window messaging redaction (multi-monitor incl. mixed-DPI, native + web
chats), trusted game detection, school-schedule import, conflict detection,
and the heartbeat watcher are all in. Later: pausing the
VLM during play (phase 3 — the real resource gating; capture keeps running),
z-order-aware partial redaction + per-URL web-chat detection, local VLM +
Claude Code bootstrap labeling (phase 3), tamper-resistant root deployment
(phase 4), enforcement via the timekpr-next policy driver + DNS/URL category
filter (phase 6, see below).

## Enforcement direction: timekpr-next + familymon

Decision (2026-08): **don't build time/session/per-app enforcement ourselves —
adopt [timekpr-next](https://github.com/polesapart/timekpr-next)** (GPL,
packaged in `ppa:mjasnik/ppa`, verified live on the target machine). It already
does, root-owned and teen-tamper-proof, what phase 6 was going to hand-roll:

- **Time windows + budgets per Linux user**: allowed hours per weekday, daily /
  weekly / monthly screen-time limits, real-time "add/set time left".
- **PlayTime = per-app budgets**: process masks (name or cmdline regex) with
  their own daily budgets and allowed days — replaces our planned game-process
  killer, and our installed-Steam-library scan produces exactly the process
  names PlayTime wants as masks.
- **Lockout mechanisms**: lock / suspend / terminate / shutdown when a limit
  or window is hit.

**Division of labor.** familymon stays the *brain* — capture, classification,
schedule conflicts, reports, and (future) DNS/URL category filtering, which
timekpr cannot do (it matches processes, not what's inside a browser). timekpr
is the *muscle* for the time/app dimension. The integration seam is a
familymon **policy driver** that translates the imported school schedule +
parent policy into timekpr settings, driving the `timekpra` admin CLI and the
system D-Bus API (`com.timekpr.server`, interfaces
`com.timekpr.server.user.admin` / `com.timekpr.server.admin`; requires root or
`timekpr` group).

**The policy driver exists** (`familymon/policy/enforcement.py` +
`timekpr_admin.py`): a parent-editable `policy.yaml` (see
`familymon/defaults/policy_example.yaml` — bedtime window, daily screen-time budgets,
game budgets) is compiled into timekpr settings, with PlayTime activity masks
generated automatically from the installed-Steam-library scan. Run on the
target machine as root:

    familymonctl enforce show <user>   # live timekpr state (read over D-Bus)
    familymonctl enforce plan          # dry-run diff, policy vs live
    familymonctl enforce apply         # guarded, minimal-diff apply

`apply` is idempotent (only deltas are written), orders changes so the
allowed-hours window lands last, and **refuses** (without `--force`) any
change that would cut off the user's active session right now — narrowing the
window past the current time, or shrinking today's budget mid-session.

**One-off grants.** `familymonctl enforce grant <user> --until 22:29` records
a date-bound exception (`grants.json` in the root state dir) and applies it at
once: today's window is widened to the minute (`22[00-29]`), the reconciler
folds active grants into its desired state so it can never revert them, and
they expire on their own. Grants only widen, so they pass the safety guard at
any hour. `--playtime 20m` adds game minutes (a counter, not config — nothing
to reconcile); `enforce grants` lists, `enforce revoke <user>` converges back
(guarded). Every grant is logged as an Event.

**Browser-game charge-back.** timekpr budgets native games by process mask
and cannot see a game inside a browser tab; familymon's classifier can. With
`games.charge_browser_games: true`, `familymon-chargeback.timer` (system,
every 5 min) deducts each new browser-game capture from PlayTime left via
`timekpra --setplaytimeleft` — the kid's DB is read read-only, a per-user
watermark prevents double charging, minutes with a native game running in
the background are skipped (timekpr already counted them), and each charge
is logged as an Event. `charge_browser_games_when` picks the rule: `visible`
(default — a game tab visible anywhere bills the minute, mirroring how
timekpr counts a native game that is merely running) or `focused` (only when
the game tab itself is focused).

Operational notes learned from the live spike (timekpr-next 0.5.10):

- PlayTime stays inert until **global** switches are flipped in
  `/etc/timekpr/timekpr.conf`: `TIMEKPR_PLAYTIME_ENABLED` and
  `TIMEKPR_PLAYTIME_ENHANCED_ACTIVITY_MONITOR_ENABLED` (the latter enables
  cmdline-regex matching — needed for wine/proton/snap-wrapped games). The
  per-user enable is silently masked until then.
- **CLI boolean display bug (0.5.10)**: `timekpra --getuserinfo` prints every
  enabled boolean as `False` (it compares `str(dbus.Boolean(True))` — `"1"` —
  against `"true"`). Write via CLI or D-Bus; **read only via D-Bus**
  (`getUserInformation(user, "F")`).
- Setting allowed hours takes effect **immediately** — changing the window
  while the teen is inside the newly-disallowed hours terminates their session
  on the spot. The policy driver must apply window changes atomically and
  outside the affected hours (or grant transition time via `--settimeleft`).

## Privacy & threat model

This is a parental-control tool with **disclosed monitoring** as a core principle. The intended deployment hands the laptop to a teen who has been told what is captured and what is not.

**Acceptable use.** familymon is for parents supervising their own minor children, openly. It is not for covert monitoring of anyone: not partners, not employees, not adults, not children who haven't been told. Monitoring another adult's device without their knowledge is abuse and, in most places, a crime. The design reflects this. Capture runs as a visible user service, the "monitoring active" indicator is meant to stay visible, messaging windows are never captured to image, and nothing leaves the machine unless the parent sends it. Changes that hide the monitoring from the monitored person will not be accepted.

Leak paths explicitly addressed:

- `/var/lib/familymon/screenshots/` — mode 0700, auto-purged (14 d during bootstrap, 24 h steady-state).
- SQLite WAL / journal — `VACUUM` nightly; image bytes never stored in the DB, only paths.
- Swap — must be disabled or encrypted on any host running familymon; otherwise screenshot bytes can land on disk uncontrolled.
- `systemd-journal` — logs capture IDs only, never window titles of messaging apps or screenshot filenames.
- Backups — exclude `screenshots/` from any backup tool.
- Claude Code bootstrap sessions — when batches of screenshots are rsynced to a labeling workstation and sent through Claude Code, those bytes traverse Anthropic infrastructure under Claude Code's data-handling terms. **The teen must be told this is happening.**
- Dashboard — LAN-only, basic auth. For remote access, prefer Tailscale over public exposure.

A messaging-app **window-class blocklist** in the capture daemon ensures private chats are never captured to image at all — only the window metadata is recorded. The list is editable by both parent and teen; treat it as a privacy contract the teen can extend.

## Packaging & delivery

The kid's laptop never builds anything and holds no repo credentials — it
just runs apt:

1. `scripts/build-deb.sh` (dev machine) → `familymon_<ver>_amd64.deb`: a
   relocatable uv venv at `/opt/familymon/venv` frozen to the committed
   `uv.lock`, `/usr/bin/familymonctl` wrapper, systemd user units (shipped
   disabled; the parent enables per user), parent-editable conffiles in
   `/etc/familymon/` (dpkg preserves local edits on upgrade), and
   `policy_example.yaml` under `/usr/share/familymon/`.
2. `scripts/publish-deb.sh` → maintains a flat apt repo (Packages +
   checksummed Release, `Origin: familymon`) and rsyncs it to the target's
   `/srv/familymon-apt` (`--setup` provisions the dir + sources entry once).
3. The target's unattended-upgrades (`Allowed-Origins "origin=*"`) installs
   familymon updates automatically — the same channel as every other package
   on that machine. `dpkg -V familymon` doubles as a file-integrity check.

Config resolution on a deployed machine: `$FAMILYMON_CONFIG_DIR/<file>` →
`/etc/familymon/<file>` → the bundled default inside the package. The
messaging-app blocklist (the privacy contract) is `/etc/familymon/
messaging_apps.yaml` — editable without touching the package.

Note on unattended timekpr upgrades (the PPA is in the target's allowed
origins): an upstream update can restart the daemon or drift its config.
`familymonctl enforce apply` is an idempotent reconciler, and the package
ships it as a **system** timer — `familymon-enforce.timer` (hourly + 5 min
after boot, ordered after `timekpr.service`; enable with
`systemctl enable --now familymon-enforce.timer`). Any drift re-converges to
`policy.yaml` within the hour; the apply guard still refuses a change that
would cut the user's live session, so a refused run simply retries later.

### Lesson-time web blocking (Chrome managed policy)

timekpr matches processes, not what's inside a browser, so the lesson-time
layer is familymon's own. While a `school` block from the imported schedule
is in progress, `familymonctl enforce web-block` (system timer
`familymon-webblock.timer`, every minute on the minute, idempotent) writes a
`URLBlocklist` into `/etc/opt/chrome/policies/managed/familymon.json`;
outside lessons the key is dropped. Chrome enforces it in-browser with its
"blocked by your administrator" page, reloads the file live (no restart),
and the file is root-owned. The block set is the configured categories of
the domain table (`web_block.categories`, default messaging + social +
games) plus `extra_domains`, minus `allow_domains`. `video` is deliberately
not blocked — a lesson may use YouTube; streaming video during a lesson is
still *flagged*. School conferencing (Meet, Zoom, Teams) is never blocked.
The always-on part of the file turns off DoH/ECH so the browser keeps using
the system resolver (groundwork for a future DNS layer; no behaviour change
today). Enable: add `web_block:` to `policy.yaml` (see
`policy_example.yaml`) and `systemctl enable --now familymon-webblock.timer`.

The reactive half lives in the kid's session: `schedule notify-tick` (the
existing per-minute user timer) also looks at the last few minutes of
classified captures and, when one conflicts with the lesson in progress —
same rules as the Conflicts panel, so this includes streaming video and
native games the policy can't block, and a chat tab that was already open —
pops a warning ("Maths — lesson in progress: Messaging seen at 09:29 …
This is logged."), at most once per 5 minutes per lesson + category. The
warning judges the *active window on its own*: a capture's stored category
is the priority verdict over every visible window, so a chat app in front
of a still-visible school page is recorded as school (and the Conflicts
panel follows that), but the warning still fires for the chat app.

A tab already open when the block starts keeps its connection (Chrome
applies the list to navigations only), so the deb also ships a tiny
force-installed Chrome extension (`familymon/chrome_extension/`, packed as a
signed CRX3 by `familymonctl extension build` at deb-build time with a
per-deployment key kept under `private/`). The same policy file
force-installs it (`ExtensionInstallForcelist`, update manifest served over
`file://` from `/usr/share/familymon/chrome-extension/`) and hands it the
block set through `3rdparty` managed storage; when a lesson starts it moves
every open tab on a blocked host to its own "blocked during the lesson" page
(Back returns after the lesson). Force-installed means the kid can neither
disable nor remove it, and `DeveloperToolsAvailability: 0` keeps DevTools
off *that extension only* — DevTools on websites stays available. Chrome
cannot push an extension into private windows, so `web_block.block_private`
disables Incognito and the Guest profile during lessons (`lessons`,
default), always, or never.

A Chrome launched by hand with `--disable-extensions` runs without the tab
closer (verified), so the lesson-time process sweep closes such a Chrome
like any other bypass.

Known gaps, deliberate for now: only Chrome is governed (other browsers are
closed during lessons, the target's unused Firefox snap was removed);
native apps are timekpr's domain unless listed in `forbid_apps`. Next
candidates, in order: a school-platform allowlist inversion (block
everything but class platforms during lessons) and the DNS layer for
non-browser apps.

## Dependency policy: current, but tested

`pyproject.toml` constraints stay deliberately loose (`>=`); `uv.lock` is
committed so a deployed machine reproduces the exact dependency set that
passed the test suite — never a fresh resolution that nobody ran.

Freshness comes from `scripts/uv-upgrade.sh` on a nightly user cron (same
pattern as the host's other upgraders): `uv lock --upgrade` → full test suite
against the upgraded set → commit + push the new lock **only on green**; on
red, the lock and the venv roll back and the run exits non-zero. Libraries
don't rot, and an untested set never reaches the kid's laptop. Log:
`~/.local/state/familymon-deps-up.log`.

## Contributing & security

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Please report security issues
privately as described in [`SECURITY.md`](SECURITY.md). That includes bypasses
that let the monitored user disable or evade enforcement without the parent
noticing.

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
