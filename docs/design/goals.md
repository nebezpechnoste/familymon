# Goals and principles

What familymon is trying to do, for whom, and the rules that shape every decision. The dated reasoning behind individual choices is in [decisions.md](decisions.md); the original plan with its phases is [../PLAN.md](../PLAN.md).

## Purpose

A parent wants to know what a child actually does on their laptop during the school day and after it, and wants limits that hold without standing behind the chair. Mainstream parental-control products don't support Linux, and the ones that exist elsewhere mostly block apps by name, which says nothing about a browser tab.

Familymon takes a **monitor-and-react** approach instead: record what is on screen every minute, classify the activity on the device, compare it with the school timetable, and act on that: a notification, a dashboard row, a blocked site during a lesson, a game budget that counts browser games too.

## Who it is for

Parents supervising their own minor children, **openly**. The child knows the laptop is monitored, knows what is recorded and what is not (chats are never captured to image), and can see the rules.

It is not for monitoring partners, employees, other adults, or children who haven't been told. Monitoring another adult's device without their knowledge is abuse and, in most places, a crime. A change whose purpose is to hide the monitoring from the monitored person will not be accepted.

The parent is assumed to operate familymon through an AI agent (see [../../AGENTS.md](../../AGENTS.md)): the parent says what they want, the agent translates it into configuration, previews, applies and verifies. There is no settings UI on purpose.

## Goals

1. **See the day as it happened.** A per-day timeline of activity categories, with the lessons overlaid, so "what did you do during Maths?" has an answer that is neither a guess nor a screenshot archive to trawl.
2. **Lesson time is lesson time.** During a scheduled lesson the obvious distractions are blocked in the browser, open tabs on them are closed, other browsers and listed apps are closed, and anything that slips through (a game, a chat app) is flagged and the child is warned on the spot.
3. **Limits that hold.** Allowed hours, daily budgets and game budgets are enforced by a root daemon the child can't stop, and browser games count against the game budget like native ones.
4. **The child can't switch it off.** Everything that decides, records or enforces runs as root with state the child can't read; what can't be prevented from inside the child's own session is detected and shown.
5. **Screenshots never leave the laptop.** Titles, app names and classifications stay on it until the parent reads them: over the LAN on the dashboard, over SSH with the CLI, or through the MCP tools into the parent's agent, which may pass them on to the model provider that agent uses. There is no upload, no cloud account and no telemetry.
6. **Cheap to run, free in the long run.** Classification uses local heuristics; there is no paid API in the loop.
7. **Operable in plain words.** The whole configuration can be derived from what the parent says, by an agent, with a preview before every change and a check after it.

## Non-goals

- **Covert monitoring.** See above.
- **Perfect enforcement.** A determined teenager with physical access can boot something else. Familymon assumes a standard (non-admin) account, locked firmware and boot loader, and a parent who looks at the dashboard. The goal is that circumvention takes effort and leaves a trace, not that it is impossible.
- **Reading the content of chats.** Messaging windows are redacted; their titles are recorded (the app and, for web chats, the page title), their content never.
- **Replacing timekpr-next.** Time and per-app budgets are timekpr's job; familymon drives it and adds what it can't see.
- **Windows, macOS, other desktops.** The capture is built on KWin (KDE Plasma 6, Wayland). Ports were considered and rejected along with commercialisation (see the decision log; the market research behind it is kept in the maintainer's private notes).

## Principles

These are the rules the code and the docs are held to. They are repeated for agents in `AGENTS.md`.

- **Disclosed monitoring.** The monitored person knows. Nothing is built to hide familymon from them.
- **The parent decides policy.** The code implements rules; it doesn't invent them. When a result looks wrong, first establish the facts, then separate a bug (fix it) from a policy question (show the parent the trade-off with real examples and let them choose).
- **Trust-resistant signals.** No signal the child controls may pause, weaken or disable monitoring. Game detection keys on what is installed, not on a flag a process can set. (A GameMode-based pause was built and reverted at once: `gamemoderun firefox` would have switched capture off.)
- **Privacy by construction, failing safe.** Chats are redacted from the window manager's geometry, never by analysing pixels; when redaction can't be done safely, the screenshot is dropped. Over-redacting is the acceptable failure; leaking is not. Raw screenshots of the child's session are never taken, not even for diagnostics.
- **Local first.** Analysis happens on the laptop. The dashboard is a LAN service, not a cloud account.
- **Root owns the truth.** State, rules and history live where only root writes. Anything read from the child's home or session is untrusted input.
- **Detect what you can't prevent.** Silence must never look like "nothing happened": a failed capture while the child is logged in is an event and a red banner, not a quiet gap.
- **Preview, apply, verify.** Every change has a dry run; every apply is checked afterwards; nothing that could end the child's live session happens without the parent saying so.
- **Current but tested dependencies.** Environments are upgraded continuously, and a locked set reaches the child's laptop only after the test suite passed against it.

## The original constraints, and where they ended up

The plan started with four hard constraints. Their status today:

| Constraint (2026-05) | Status |
|---|---|
| Free in the long run; a bootstrap phase labels captures through a Claude Code subscription, never the API | Classification stayed heuristic; the local vision model and the bootstrap labeling were never built. No API, no subscription in the loop. |
| Analysis must not steal resources during allowed play | Capture keeps running during games (a capture-disable trigger would be an off-switch). The resource-heavy stage that would have been paused doesn't exist. |
| Screenshots stay on the laptop they were captured on | Holds. Nothing uploads them. |
| MVP is monitoring and reports; blocking comes later and the design accommodates it | Blocking shipped: timekpr-next for time and games, Chrome policy and a process sweep for lessons. |

The plan also specified screenshot retention (14 days, then 24 hours) and a nightly `VACUUM`. Retention shipped on 2026-10-19 as a daily job with a 90-day default (`retention.screenshots_days`; the parent can set the plan's shorter windows). `VACUUM` was never needed: image bytes are never stored in the database.
