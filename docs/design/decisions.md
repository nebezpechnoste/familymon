# Decision log

Dated decisions with the reasoning, newest last. Each entry says what was decided, why, and what it changed. Family-specific policy choices (this household's bedtimes, budgets, excluded subjects) are deliberately not here; they live in the household's own `policy.yaml` and schedule extras.

## 2026-05 · Monitor and react, locally

**Decided:** capture screenshots and the window list every minute, classify on the device, compare with the school timetable, and only then act. Not a block-list-first tool.

**Why:** a block list can't see inside a browser, and the parent wanted to know what actually happened, not only to forbid. Local analysis means private content never leaves the laptop, which also removes any running cost.

**Consequences:** the capture pipeline, the classifier and the schedule model came first; enforcement was designed in but built later. See [../PLAN.md](../PLAN.md).

## 2026-05-27 · One primary category per capture, by priority over all visible windows

**Decided:** each capture gets exactly one category, chosen by a fixed priority order over every visible window, not only the focused one. The full priority-ordered set of categories present is recorded alongside (`present_categories`).

**Why:** a focused chat window must not hide a visible game or video. A single category keeps the timeline readable and the statistics summing to wall-clock time. Recording the present set costs nothing and keeps the door open for overlap-aware time accounting later.

**Consequences:** the "also: …" annotation on the dashboard; overlap-aware time statistics remain deferred.

## 2026-05-27 · No off-switch, ever: GameMode gating reverted

**Decided:** nothing the child controls may pause or weaken monitoring. A GameMode-based pause, built shortly before, was removed. Game detection keys on the installed Steam library (real `appmanifest` entries) plus a parent-curated list, matched against running processes. Capture keeps running during games.

**Why:** `gamemoderun firefox` registers Firefox as a game, and the pause would have switched screenshots off while the child browsed. A monitored user holding the switch is backwards.

**Consequences:** the trust-resistant-signals principle; `familymon/capture/games.py`.

## 2026-06 · Perceptual-hash dedup; redact regions instead of dropping frames

**Decided:** a screenshot is persisted only when it is visually novel (pHash against the last few), not when the active window changed. When a chat window is visible, its rectangle is blacked out and the rest of the frame is kept; if the rectangle can't be mapped safely (non-uniform scaling, unknown geometry), the whole screenshot is dropped.

**Why:** a playing video keeps one window title while its content changes every second, so same-window dedup suppressed exactly the frames that mattered. The original privacy rule skipped the screenshot only when the *foreground* window was a chat: a chat in the background was captured in full, and a chat in front hid whatever was behind it, so a game behind a chat went unobserved. Blacking out every visible chat's rectangle fixes both without analysing its pixels.

**Consequences:** `capture/loop.py`, `capture/redact.py`, the uniform-scale guard, the privacy check in `familymonctl health`.

## 2026-08 · Adopt timekpr-next for time and per-app enforcement

**Decided:** don't build time windows, daily budgets, per-app budgets or lockouts; drive [timekpr-next](https://github.com/polesapart/timekpr-next) from a parent-editable policy file. Familymon stays the brain (capture, classification, schedule, reports, browser-level blocking); timekpr is the muscle.

**Why:** timekpr already does it, root-owned and tamper-resistant, with lock/suspend/terminate lockouts and per-process budgets whose masks our Steam-library scan produces for free. Hand-rolling it would have been months of work with worse edge cases.

**Consequences:** the policy driver (`enforce plan/apply`), reconciled hourly with a guard that refuses to cut a live session; date-bound grants that the reconciler never reverts; the PlayTime masks generated from installed games. Operational findings with timekpr 0.5.10 (global PlayTime switches, the CLI boolean display bug) are in the troubleshooting guide.

## 2026-08 · Ship a deb with a frozen, bundled environment

**Decided:** the child's laptop installs a `.deb` from a flat apt repo through its normal unattended upgrades. The package bundles a relocatable virtualenv frozen to the committed `uv.lock`; the laptop builds nothing and holds no repository credentials.

**Why:** the deployed machine should run exactly the dependency set that passed the tests, never a fresh resolution nobody ran; and upgrades should arrive the same way as every other package.

**Consequences:** `scripts/build-deb.sh`, `scripts/publish-deb.sh`, the nightly lock upgrader that commits only on green.

## 2026-10-05 · Browser games are charged to the game budget; the default rule is "visible"

**Decided:** minutes where the classifier sees a browser game are deducted from timekpr's PlayTime budget. By default a game tab visible anywhere on screen bills the minute; the stricter "focused" rule is available.

**Why:** timekpr budgets native games by process and can't see a game in a tab, which made browser games free. "Visible" mirrors how timekpr treats a native game that is merely running.

**Consequences:** `enforce chargeback` on a 5-minute timer, a watermark so a minute is billed at most once (it resets if the database is replaced), and no double charge while a native game is running.

## 2026-10-12 · Lesson-time web blocking through Chrome policy; video flagged, not blocked

**Decided:** during a scheduled lesson, Chrome's managed policy carries a URL blocklist of the configured categories (messaging, social, games by default). Streaming video is flagged in the timeline and the live warning but **not** blocked; school conferencing is never blocked. Other browsers and listed apps are closed while a lesson runs, rather than governed. The always-on part of the policy turns off DNS-over-HTTPS and ECH, as groundwork for a DNS layer that does not exist yet.

**Why:** timekpr matches processes, not what is inside a browser. A lesson may legitimately use YouTube, so blocking video would break classes; flagging keeps it visible. Governing every browser means maintaining every browser's policy format; closing the others is simpler and the child uses Chrome for school anyway.

**Consequences:** `enforce web-block` every minute, the lesson sweep, `forbid_apps`.

## 2026-10-13 · A force-installed extension closes tabs that were already open

**Decided:** the package ships a small Chrome extension, signed at build time and force-installed by the same policy file, that moves open tabs on blocked hosts to a "blocked during the lesson" page when a lesson starts. Developer tools are disabled for that extension only. Private windows (Incognito, Guest) are disabled during lessons by default, because policy can't push an extension into them.

**Why:** the URL blocklist applies to navigations, so a chat tab opened before the lesson kept working through it.

**Consequences:** `familymon/chrome_extension/`, `familymonctl extension build`, `web_block.block_private`. A Chrome started with `--disable-extensions` is treated as a bypass and closed by the sweep.

## 2026-10-19 · Open source instead of a product

**Decided:** publish under GPL-3.0-or-later; no commercialisation, no ports to Windows or macOS.

**Why:** the reachable market (homeschooling and online-school families who want this on a desktop) is small and crowded with free OS features; the author does not want to sell software to other parents; and a privacy-sensitive monitoring tool is more credible when its code can be read. Research: [../research/commercialization-2026-10.md](../research/commercialization-2026-10.md).

**Consequences:** the public repository starts from one clean commit; family-specific configuration lives outside it; the docs are written for any parent's AI agent.

## 2026-10-19 · The documentation is written for an AI agent

**Decided:** there is no settings UI. The docs are organised by the task an agent is asked to do (install, configure from the parent's intent, verify, troubleshoot, look up, change the code), with [`AGENTS.md`](../../AGENTS.md) as the entry point and a short list of rules that are never optional.

**Why:** the parent already operated familymon this way, and it is the only setup path that scales to "paste a timetable and say what you want".

## 2026-10-19 · Everything runs as root; capture through a per-tick helper

**Decided:** every unit is a system unit running as root; all state lives in `/var/lib/familymon`, which the child can't read. Capture is a root supervisor that follows the child's session through logind and, once per tick, runs a short-lived helper under the child's uid to ask KWin for the window list and the screenshot. The dashboard runs as a dedicated unprivileged user, read-only and sandboxed. Rejected alternatives: a dedicated system user for capture (it can't reach the session bus at all), running the capture as root directly (KWin only serves its own session's clients), KDE Kiosk lockdown (UI-only, not a boundary). Automatic escalation when tampering is detected (for example locking the session) was left out on purpose: it is policy, and the parent hasn't asked for it.

**Why:** the child can kill any process under their own uid and can mask any user unit, including global ones. Before this change they could stop capture, delete their history, edit the schedule to switch lesson blocking off, recreate the database to escape browser-game billing, forge the window list, and read the dashboard password.

**Consequences:** `familymon/capture/supervisor.py`, the window-list sink that trusts only KWin's own connection plus a per-tick nonce, `capture_failed` and `capture_tamper` events, a session-aware heartbeat, `migrate-state` for existing installs, remembered game directories. Full write-up: [../develop/tamper-resistance.md](../develop/tamper-resistance.md).

## 2026-10-19 · A window on another virtual desktop is not on screen

**Decided:** windows on other virtual desktops or activities are treated like minimized ones: not redacted, not counted as present by the classifier. Missing desktop information still counts as on screen.

**Why:** a chat parked on desktop 1 was blacking out a rectangle of whatever desktop 2 showed, tagging every capture as "messaging present", and counting as a visible app for classification. Over-redaction remains the failure mode when KWin can't say where a window is.

## 2026-10-19 · Screenshots are kept 90 days by default

**Decided:** a daily root job deletes screenshot files older than `retention.screenshots_days` (default 90; `0` keeps them forever), judged by the date of their day directory, and clears the paths on the affected capture rows. Capture rows, titles and classifications are kept; they are about a kilobyte each.

**Why:** screenshots were never purged (the plan's 14-day and 24-hour windows belonged to a labeling pipeline that was never built) and had reached 3.3 GB after three weeks on the deployed laptop, now on the root filesystem. Three months keeps a term's worth of history to look back over while bounding both disk use and the amount of sensitive imagery at rest. The parent sets the number.

**Consequences:** `familymonctl retention`, `familymon-retention.timer`, the `screenshots_pruned` event, and a `retention` section in `policy.yaml`.

## 2026-10-19 · Personal time: no screenshots and no titles after an evening hour, game time still counts

**Decided:** `personal_time` in `policy.yaml` names a daily window, from a clock time until that day's bedtime lock (or an explicit end). Inside it the capture takes no screenshot and flags the row; the dashboard and its API show only categories and durations for the period, no app names or titles. Classification and the browser-game chargeback run on those rows as usual. Rejected: pausing capture entirely (browser games would become free time, and the parent would lose the only signal that counts them), and keeping the screenshots but hiding them (every reader of captures would need its own exclusion, and the images would still sit on disk).

**Why:** the parent's words: "I don't want to peek into what he watches, but I don't want to lose control over gaming time either." Titles are the classification signal, so they stay in the root-only database; what changes is what the parent looks at.

**Consequences:** `policy/personal_time.py`, the `personal_time` capture column, a per-tick gate in the supervisor, masking in the dashboard, `familymonctl personal-time`, and a once-a-day notice to the child. Lessons inside the window are still blocked and warned about, and `familymonctl conflicts` is not masked.

## 2026-10-19 · No port to SteamOS (Steam Machine, Steam Deck); no publication on Steam

**Decided:** familymon is not ported to SteamOS and is not offered through Steam. Evaluated after the parent asked how hard a port would be and whether it could be published as a Steam addon.

**Why:** Desktop Mode would be the easy part. SteamOS 3.8 runs Plasma 6.4 on Wayland by default, so the capture path (KWin scripting, spectacle, the root supervisor with the per-tick helper) would work unchanged. Everything around it would not. The root filesystem is replaced on every update, and only an allow-list of `/etc` files survives (extendable through `/etc/atomic-update.conf.d`), so the whole install would have to live under `/var/lib` with its own Python, and the root units, the service user and the sudoers line would need keep-list entries. timekpr-next is not available there and could not persist, so the bedtime, daily-limit and game-budget enforcement has no muscle. Browsers are Flatpaks, so the Chrome managed policy and the force-installed extension would need sandbox plumbing. Gaming Mode, where a child actually plays, runs under gamescope with no KWin and no spectacle: a new backend would have to read the running title from the Steam launcher's process tree, and the only enforcement levers would be suspend or killing the game. Publishing on Steam has no viable shape: a Software listing runs as the device user from the Steam client, cannot keep a root service and can be uninstalled from the library; Steam has no plugin API for system tools; the community channel, Decky Loader, breaks on SteamOS updates and is reinstalled by the device's user, so it cannot carry parental control. Valve's own Steam Families already gives child accounts playtime limits, reports, extension requests, store, community and chat restrictions and age-based game access, account-level and server-side, so it works on every device and cannot be defeated with local root.

**Consequences:** on a Steam Machine or Deck the parent uses Steam Families. Familymon's value, screen-content classification, lesson-time blocking and chat redaction, stays on the school laptop. The one piece that might be worth writing later is a Gaming Mode title tracker without screenshots (about a day), for non-Steam apps added to the library, which Steam Families does not see. Rough effort if ever revisited: Desktop Mode monitoring a few days; enforcement on SteamOS not worth building.
