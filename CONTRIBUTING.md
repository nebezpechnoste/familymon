# Contributing to familymon

Thanks for your interest. familymon is a small project maintained in spare
time, so please open an issue to discuss anything bigger than a bug fix before
you write code.

## Ground rules

- **Disclosed monitoring only.** See *Acceptable use* in the README. Features
  whose purpose is to hide monitoring from the monitored person will be
  declined.
- **Trust-resistant signals.** Never let the monitored user control a signal
  that turns monitoring or enforcement off. (Example: GameMode was rejected as
  a "game running" signal because anyone can wrap any process in
  `gamemoderun`.) Detect from installed or system state instead.
- **Privacy by default.** Screenshots stay on the machine that captured them.
  Messaging windows are never captured to image. Don't add network egress
  without an explicit, documented opt-in.
- **Tests never touch real processes or real user state.** Destructive code
  paths (killing processes, applying timekpr limits, writing browser policy)
  must be tested only against things the test itself spawned or created in a
  temp dir.

## Development

```bash
uv sync --all-extras
uv run pytest -q             # Python suite
node --test tests/js/*.test.mjs   # Chrome extension rules (also run by pytest)
```

`uv.lock` is committed and refreshed nightly by `scripts/uv-upgrade.sh`, but
only when the tests pass. Keep `pyproject.toml` constraints loose (`>=`).

## Pull requests

- Keep each PR to one focused change, with tests for new behaviour.
- In the description, say what you verified and on which desktop (KDE
  Wayland, X11, …).
- By contributing you agree that your contribution is licensed under
  GPL-3.0-or-later.
