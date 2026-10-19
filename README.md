# familymon

Parental-control software for a child's Linux laptop (KDE Plasma 6 on Wayland). It records what is on screen, classifies the activity on the device, compares it with the school timetable, and enforces time, game and lesson-time limits. Everything stays on the laptop.

Familymon has no settings UI. An AI agent acting for the parent (for example Claude Code) is the interface: the parent says what they want, in words or by pasting a timetable, and the agent turns that into configuration, previews it, applies it, verifies it and explains it back. If you are that agent, start at [`AGENTS.md`](AGENTS.md).

## Who it is for

Parents supervising their own minor children, **openly**. The child knows the laptop is monitored and knows what is recorded. Familymon is not for covert monitoring of anyone: not partners, not employees, not adults, not children who haven't been told. Monitoring another adult's device without their knowledge is abuse and, in most places, a crime. Changes that hide the monitoring from the monitored person will not be accepted. The goals and principles are in [docs/design/goals.md](docs/design/goals.md).

## What it does

- **Captures** the window list and a screenshot every 60 s, keeping a screenshot only when it differs from the recent ones. No screenshot while the screen is locked or the session is in the background.
- **Redacts chats before anything is stored.** Messaging windows (native apps by window class, web chats by page title) are blacked out of the screenshot using the window manager's geometry, never by looking at pixels. If a chat window can't be mapped safely, the whole screenshot is dropped. Windows on other virtual desktops count as not on screen, like minimized ones.
- **Classifies** each capture locally with cheap heuristics: application categories from desktop files, domain categories from a bundled list, brand keywords in browser titles. One primary category per capture, chosen by a priority rule over all visible windows (a focused chat can't hide a visible game), plus the full set of categories present.
- **Knows the school timetable.** Imports the weekly schedule (Kings InterHigh student-portal JSON is the supported format), merges parent extras (subjects to ignore, custom blocks, holidays), and flags distraction during lessons: games, social media, messaging, and streaming video from known platforms. Plain browsing is not flagged, because lessons run in the browser.
- **Shows** a per-day timeline, the lesson conflicts and capture health on a LAN dashboard, and pops desktop notifications when a block starts and when a distraction is seen during a lesson.
- **Enforces** through [timekpr-next](https://github.com/polesapart/timekpr-next): allowed hours, daily budgets, and game budgets for games detected from the installed Steam library plus a parent list. A policy file is reconciled every hour, with a guard that refuses to cut a live session; one-off grants extend a day without being reverted; browser-game minutes, which timekpr can't see, are charged to the same budget.
- **Blocks during lessons** with Chrome's managed policy: a URL blocklist by category, a force-installed extension that closes tabs already open on blocked sites, private windows off. Other browsers and listed apps are closed while a lesson runs.
- **Resists tampering.** Everything runs as root with state the child can't read; the only familymon code that runs under the child's account is a short-lived capture helper, once per tick. What the child can still do (log out, kill the helper) is detected and shown, never silent. See [docs/develop/tamper-resistance.md](docs/develop/tamper-resistance.md).
- **Checks itself.** A root heartbeat knows whether the child is logged in; `familymonctl health` audits the data, including a hard privacy check; a damaged database recovers itself when opened.

## Privacy

What the code does:

- Screenshots and metadata never leave the laptop. There is no upload, no cloud account, no telemetry.
- Messaging windows are redacted at capture time. `familymonctl health` fails if a screenshot with a chat present ever reaches disk unredacted.
- Screenshots are deleted after 90 days by a daily job (`retention.screenshots_days` in `policy.yaml`; `0` keeps them). Capture rows and classifications stay, so the timeline keeps its history without the images.
- Personal time: from an evening hour the parent picks (`personal_time` in `policy.yaml`) until the bedtime lock, no screenshots are taken and the dashboard shows only categories and durations, no app names or titles. Game time still counts.
- Window titles of everything on screen are recorded; they are the classification signal. They live in a database only root and the read-only dashboard can open. Logs carry capture ids, states and errors.
- The dashboard binds to localhost unless configured otherwise, uses HTTP basic auth, and has no TLS: expose it on a trusted LAN or over a VPN such as Tailscale, never on the internet.

What the host should do: encrypt the disk; keep swap off or encrypted, because screenshot bytes pass through memory; exclude `/var/lib/familymon/screenshots` from backups.

## Requirements

- A Linux laptop with systemd and KDE Plasma 6 on Wayland. The capture uses KWin's scripting interface and Spectacle. Developed and used on Ubuntu; shipped as a `.deb`.
- Python 3.12 or newer (the package bundles its own environment).
- timekpr-next for time and game enforcement; Google Chrome for lesson-time blocking.
- The child uses a standard account without sudo, and the firmware and boot loader are password-protected. Familymon does not defend against root or physical access.

## Install and operate

Build the package on another machine (`scripts/build-deb.sh`), publish it to a flat apt repo (`scripts/publish-deb.sh`), install it on the child's laptop, write `/var/lib/familymon/policy.yaml` from the shipped example, and import the timetable. The step-by-step guide is [docs/operate/install.md](docs/operate/install.md). From there, configuration is a conversation with the agent: [docs/operate/configure-from-intent.md](docs/operate/configure-from-intent.md).

## Documentation

| | |
|---|---|
| [AGENTS.md](AGENTS.md) | Entry point for an AI agent operating familymon, and the rules that are never optional |
| [docs/concepts.md](docs/concepts.md) | How the parts fit together |
| [docs/operate/](docs/operate/) | Install, configure from the parent's intent, verify, troubleshoot |
| [docs/reference/](docs/reference/) | CLI, configuration, files, units and events |
| [docs/develop/](docs/develop/) | Architecture, tamper resistance, testing and shipping |
| [docs/design/](docs/design/) | Goals and principles, and the decision log with the reasoning |
| [docs/PLAN.md](docs/PLAN.md) | The original plan, kept for history; parts of it are superseded |

## Status and known gaps

Familymon is in daily use in one household. Gaps, deliberate for now:

- Only Chrome is governed during lessons; other browsers are closed, not filtered. There is no DNS layer, so what non-browser apps do on the network is timekpr's business (process budgets), not familymon's.
- The lesson rule flags known entertainment video and allows everything else. The planned inversion (allow only class platforms during lessons) is not built.
- Classification is heuristic only. The local vision-model and OCR stages of the original plan were never built; `pytesseract` is still declared as a dependency for the OCR stage and is unused.
- The live lesson warning only sees captures that the 5-minute classification pass has labelled, so a short distraction can escape the warning. It still shows in the timeline.
- The dashboard has no TLS.

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report security issues privately as described in [`SECURITY.md`](SECURITY.md); that includes any way for the monitored user to disable or evade enforcement without the parent noticing.

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
