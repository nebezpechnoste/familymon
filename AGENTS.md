# familymon — guide for AI agents

familymon is local-first parental-control software for a Linux laptop (KDE Plasma 6, Wayland) used by a child. It watches what is on screen, classifies the activity on the device, compares it with the school timetable, and enforces time, game and lesson-time limits through timekpr-next and Chrome managed policy.

**familymon has no settings UI. An AI agent acting for the parent *is* the UI.** The parent states what they want in any form: plain words, a PDF or a screenshot of a timetable, a school-portal export. You turn that into configuration, preview it, apply it, verify it, and explain it back. This file tells you where to start. The `docs/` tree has the detail.

## Read this first, by task

| You were asked to… | Read |
|---|---|
| Understand what familymon is and how the parts fit together | [docs/concepts.md](docs/concepts.md) |
| Install it on a kid's laptop | [docs/operate/install.md](docs/operate/install.md) |
| Turn the parent's wishes (bedtime, game time, lesson blocking, a timetable, a holiday, "one more hour tonight") into config | [docs/operate/configure-from-intent.md](docs/operate/configure-from-intent.md) |
| Check that a change worked, or that everything is healthy | [docs/operate/verify-and-health.md](docs/operate/verify-and-health.md) |
| Find out why something doesn't work | [docs/operate/troubleshoot.md](docs/operate/troubleshoot.md) |
| Look up an exact command, option, config key, path, unit or event | [docs/reference/cli.md](docs/reference/cli.md), [docs/reference/config.md](docs/reference/config.md), [docs/reference/files-and-units.md](docs/reference/files-and-units.md) |
| Change the code | [docs/develop/architecture.md](docs/develop/architecture.md), [docs/develop/testing-and-shipping.md](docs/develop/testing-and-shipping.md) |

`README.md` gives the human overview and the list of known gaps. `docs/PLAN.md` is the original design plan, kept for its reasoning; parts of it are superseded.

## Rules that are never optional

1. **The parent decides policy.** You translate, preview and explain. When a result surprises someone, first establish the facts from the data. Then separate a **bug** (the code didn't do what the rule says; fix it) from a **policy question** (the rule itself; show the parent the trade-off with real examples and let them choose). Never change classification or enforcement policy on the strength of one ambiguous data point.
2. **Disclosed monitoring only.** The child knows they are monitored. Never configure, build or suggest anything meant to hide monitoring from the person being monitored. familymon is not for monitoring partners, employees or other adults.
3. **Never weaken monitoring on the child's request alone.** Relay it to the parent. Never add a signal the child can control that pauses or disables monitoring. (GameMode was rejected because `gamemoderun firefox` would have switched capture off.)
4. **Never take a raw screenshot of the child's session**, not even for diagnostics. It bypasses the messaging-window redaction, which is the core privacy promise. Verify through CLI output, the desktop's notification history, D-Bus state, or familymon's own redacting capture.
5. **Preview, apply, verify, in separate steps.** Use the dry-run or plan variant first. Check real exit codes. Never pipe a test run or check into the next action (`pytest | tail && build` hides failures and has shipped a red build).
6. **Don't cut a session off by surprise.** The enforcement guard refuses changes that would end the child's live session. Use `--force` only when the parent explicitly says so. Use date-bound grants (`familymonctl enforce grant`) for one-off extensions, never raw `timekpra` edits; the hourly reconciler reverts those.
7. **Read the child's data read-only.** Open their capture DB with SQLite `mode=ro`. Never edit either database by hand.
8. **Tests and dev runs never touch real processes or real user state.** Anything that kills processes, writes browser policy or calls timekpr is tested only against what the test itself created.

## Operating conventions

- Commands run on the kid's laptop, usually over SSH from the parent's machine. System-wide state (policy, grants, Chrome policy) needs root. The child's own state (schedule, captures) lives in their `~/.local/state/familymon/`, so run schedule commands as the child: `sudo -u kid -H familymonctl …`.
- The docs use placeholders: the monitored account is `kid`, the laptop is `kid-laptop`, addresses come from `192.0.2.0/24`, and the timezone is `Europe/Berlin`. Substitute the real values for the family you're helping, and never write those real values back into this repository.
- After any change, report to the parent in plain words: what changed (file, key, old and new value), what the preview said, how you verified it, and how to undo it.
- Facts here are checked against the code. Statements about timekpr-next behaviour that familymon's code can't show (for example its ~3 s poll) are marked as observed with a specific timekpr version. If code and docs disagree, the code wins: fix the doc in the same change.

## Developing

- Python ≥3.12 with `uv`; `uv.lock` is committed. Tests: `uv run pytest -q`, which also runs the Chrome-extension tests under Node when Node is installed. CI runs the same on every push.
- Keep the privacy design intact: screenshots never leave the machine they were captured on, and messaging windows are never captured to image.
- Never commit anything that identifies a real family: names, hosts, addresses, schedules, timezones. Tests and examples use the placeholders above. The maintainer's push is additionally guarded by `.githooks/pre-push` against a private denylist.
- Details: [docs/develop/testing-and-shipping.md](docs/develop/testing-and-shipping.md).
