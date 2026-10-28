# MCP server reference

Read this when you set up the parent's AI agent to drive familymon through MCP tools instead of running `familymonctl` over SSH, or when you need the exact tool names, parameters, results, annotations and error behaviour of `familymonctl mcp`. The commands behind each tool are in [cli.md](cli.md); policy and config keys in [config.md](config.md); files, events and the sudoers rule in [files-and-units.md](files-and-units.md).

Placeholders: the monitored account is `kid`, the laptop is `kid-laptop`, the parent's own account on the laptop is `parent`.

## 1. What it is and the trust model

`familymonctl mcp` serves familymon's operator surface as MCP tools over stdin and stdout (stdio transport only). The parent's MCP client starts it on the laptop over SSH, `ssh kid-laptop sudo -n familymonctl mcp`, and talks to it through that pipe; the server runs as root and exits when the client closes stdin. Nothing listens on the network. The trust is the parent's SSH key plus one sudoers line that lets members of the `familymon-admin` group start exactly `/usr/bin/familymonctl mcp` as root without a password. The child's account cannot reach any of it. An HTTP transport was considered and rejected: it would give the child a root-privileged control surface on localhost (see [../develop/tamper-resistance.md](../develop/tamper-resistance.md)). Every tool calls the same function in `familymon/ops.py` as the corresponding CLI command, so the two surfaces cannot drift apart; where that command has `--json`, the tool's result is exactly what it prints.

## 2. Setup on the laptop

The deb does the system part:

| Item | Detail |
|---|---|
| `/etc/sudoers.d/familymon-mcp` | Mode 0440, a dpkg conffile, checked with `visudo -c` at build time. One rule: `%familymon-admin ALL=(root) NOPASSWD: /usr/bin/familymonctl mcp`. Because arguments are given, sudo allows that exact command line and nothing else (not even `familymonctl mcp --help`). |
| group `familymon-admin` | System group created by the postinst, empty. |
| `mcp` Python extra | `mcp>=2.3`, bundled in `/opt/familymon/venv`. |

The parent adds their own account to the group (it takes effect at their next SSH login):

```bash
sudo usermod -aG familymon-admin parent
```

Safety: the monitored account must never be a member of `familymon-admin`. Membership means a password-less root process that can rewrite the policy and change enforcement.

Check, on the laptop:

```bash
getent group familymon-admin          # members: the parent only
sudo -l -U parent | grep familymonctl # shows the NOPASSWD rule
```

Check from the parent's machine (`--help` is answered by the CLI without starting the server and needs no root; the second line starts the server with stdin closed, so it exits 0 at once if sudo let it run):

```bash
ssh kid-laptop familymonctl mcp --help
ssh kid-laptop sudo -n familymonctl mcp </dev/null; echo "exit=$?"
```

`sudo: a password is required` means the account is not in the group yet (or has not logged in again since being added).

Dev checkout: the extra is optional. Install it with `uv sync --extra mcp`, or run `uv run --extra mcp familymonctl mcp`. Without it `familymonctl mcp` exits 1 with `the MCP server needs the 'mcp' extra (uv sync --extra mcp): ...`. A non-root dev run works on the dev state directory, not `/var/lib/familymon` (see [cli.md](cli.md)).

## 3. Setup on the parent's machine

Claude Code:

```bash
claude mcp add --scope user familymon -- ssh kid-laptop sudo -n familymonctl mcp
```

Any other MCP client takes the same stdio server definition: command `ssh`, arguments `kid-laptop sudo -n familymonctl mcp`. As JSON (the shape most clients use):

```json
{"mcpServers": {"familymon": {"command": "ssh", "args": ["kid-laptop", "sudo", "-n", "familymonctl", "mcp"]}}}
```

The SSH login must work without a prompt (key-based, host key already accepted), because the client cannot answer one. `-n` makes sudo fail instead of waiting for a password.

### 3.1 Per-tool permissions

Recommended: let the client run the read-only tools without asking, and keep a prompt on everything else.

| Tools | Recommendation | Why |
|---|---|---|
| `status`, `health`, `events`, `timeline`, `personal_time`, `schedule_show`, `policy_get`, `policy_validate`, `config_get`, `enforce_show`, `enforce_plan`, `grants_list`, `games_list` | allow | Change nothing. |
| `policy_set`, `config_set` | prompt | Replace a file; the previous one is kept as `.prev`. Nothing is applied. |
| `schedule_import`, `schedule_refresh`, `games_forget` | prompt | Replace `schedule.yaml` or the remembered-games list; the previous one is kept as `.prev`. |
| `enforce_apply`, `grant`, `grant_revoke` | prompt, always | Change what timekpr enforces right now. |

In Claude Code the tools are named `mcp__familymon__<tool>`, so the allow list in `~/.claude/settings.json` looks like `"permissions": {"allow": ["mcp__familymon__status", "mcp__familymon__health", ...]}` with the thirteen read-only names.

## 4. Tools

Class and annotations (the hints the server declares; every tool has `open_world_hint: false`):

| Class | Annotations |
|---|---|
| read-only | `read_only_hint: true` |
| write | `read_only_hint: false`, `destructive_hint: false`, `idempotent_hint: true` |
| enforcing | `read_only_hint: false`, `destructive_hint: true`, `idempotent_hint: false` |

`?` marks an optional parameter. Days are `YYYY-MM-DD` strings, default today. Results are JSON objects; the keys listed are the top-level ones.

| Tool | Class | Parameters | Returns | CLI equivalent |
|---|---|---|---|---|
| `status` | read-only | none | `version`, `package_version` (deb, or null), `uid`, `state_dir`, `config_dir`, `monitored_user`, and `{path, exists}` for `db`, `screenshots_dir`, `policy`, `schedule` | `status --json` |
| `health` | read-only | none | `ok`, `exit_code` (0, 2 privacy violation, 4 damaged DB), `report` (the health numbers), `warnings`, `failures`, `heartbeat` (`status`, `session`, `age_seconds`, `last_capture_at`, `checked_at`), `capture_problems_today` | `health --json` |
| `events` | read-only | `day?`, `kinds?` (list) | `day`, `count`, `events` (`at`, `kind`, `data`). Heartbeats only when `kinds` includes `heartbeat` | `events --json [--day D] [--kind K]...` |
| `timeline` | read-only | `day?` | `date`, `blocks` (category, app, title, start, end, duration; app and title null during personal time), `total_captures`, `seconds_per_category`, `conflicts`, `personal_time_spans`, `school_break`, `heartbeat`, `capture_problems`. Never an image | none (the dashboard's day page) |
| `personal_time` | read-only | `day?` | `now`, `window` (`start`, `end`, `ends_at_bedtime`, `label`, or null), `active_now`, `day`, `hidden_captures`, `hidden_game_captures`, `hidden_game_minutes` | `personal-time --json [--day D]` |
| `schedule_show` | read-only | none | `path`, `exists`, `timezone`, `blocks` (`day`, `start`, `end`, `subject`, `category`; `group_name` and `notify` when set), `breaks` (with `label`, `in_progress`), `count`, `by_category` | `schedule show --json` |
| `policy_get` | read-only | none | `path`, `exists`, `text` (the file, comments included), `ok`, `errors`, `summary` | `policy get --json` |
| `policy_validate` | read-only | `text` | `ok`, `errors` (one per failing section), `summary` (per-day `allowed_hours` and `daily_limits` as `HH:00-HH:59` and `unlimited`/`<n>s`, `lockout`, `games`, `personal_time`, `web_block`, `retention_screenshot_days`) | `policy validate --json -` |
| `config_get` | read-only | `name` | `name`, `path` (the file in effect), `overridden`, `override_path`, `text`, `takes_effect` | `config get NAME` (text only) |
| `enforce_show` | read-only | `user?` (default: the policy user) | `user`, `active_sessions`, `lockout`, `allowed_weekdays`, `allowed_hours`, `daily_limits`, `playtime` (`enabled`, `daily_limits`, `activities`), read live over D-Bus | `enforce show --json [USER]` |
| `enforce_plan` | read-only | none | `user`, `changes` (`setting`, `live`, `desired`), `count`, `up_to_date`, `safe_now`, `reasons` | `enforce plan --json` |
| `grants_list` | read-only | none | `grants` (`user`, `day`, `start`, `end`, `note`), `count` | `enforce grants --json` |
| `games_list` | read-only | none | `games` (`dir`, `installed`), `count` | `enforce games --json` |
| `policy_set` | write | `text` (the whole document) | `path`, `changed_keys`, `previous_saved_to`, `summary`, `takes_effect` (per changed top-level key) | `policy set -` |
| `config_set` | write | `name`, `text` | `name`, `path`, `previous_saved_to`, `takes_effect` | `config set NAME -` |
| `schedule_import` | write | `data_json` (the export as JSON text), `format?` (default `kingsinterhigh`, the only one) | the `schedule_show` keys plus `previous_saved_to` and `imported` (`extras`, `excluded_subjects`, `breaks`) | `schedule import FILE` |
| `schedule_refresh` | write | none | same as `schedule_import` | `schedule refresh` |
| `games_forget` | write | `dirs` (list) | `forgotten`, `remaining`, `previous_saved_to` | `enforce games --forget DIR...` |
| `enforce_apply` | enforcing | `force?` (default false) | `user`, `applied` (`setting`, `live`, `desired`), `count`, `up_to_date`, `forced` (true only when force overrode an unsafe verdict) | `enforce apply [--force]` |
| `grant` | enforcing | `user`, `until?` (`HH:MM`), `from_time?` (`HH:MM`, default now), `playtime?` (`30m`, `1h`), `note?`; give `until` or `playtime` or both | `user`, `hours` (`day`, `start`, `end`, `timekpr_changes`) when `until` was given, `playtime_seconds` when `playtime` was given | `enforce grant [--until] [--from] [--playtime] [--note] USER` |
| `grant_revoke` | enforcing | `user` | `user`, `revoked` (count), `applied`, `deferred`; `reasons` when deferred | `enforce revoke USER` |

Side effects match the CLI commands (see [cli.md](cli.md)): `policy_set` writes event `policy_changed`, `config_set` writes `config_changed`, `enforce_apply` writes `policy_applied` when it changed something, `grant` writes `grant_hours` and/or `grant_playtime`. `policy_set` applies nothing: hours, limits, lockout and game budgets need `enforce_plan` then `enforce_apply` (or the hourly reconciler). For `schedule_extras.yaml`, run `schedule_refresh` after `config_set`. `grant` only widens, so it has no guard; `grant_revoke` narrows only when that is safe now, otherwise it returns `deferred: true` and the reconciler converges later.

## 5. Errors

| Failure | Result |
|---|---|
| A failure the parent should read as a sentence (`OpsError` in `familymon/ops.py`: no policy, invalid policy or config, unknown config name, bad day or time, unknown user, unparsable export, ...) | `is_error: true` with the text `Error executing tool <name>: <message>`. On the CLI the same message is a click error, exit 1. |
| `enforce_apply` would end the child's live session and `force` is not true | `is_error: true`; the message after the prefix starts `refusing to apply — it would cut off an active session:` and lists the reasons. On the CLI the same case exits 3. Nothing is applied. |
| Arguments that do not match the tool's schema | `is_error: true` with the validation message. |
| `schedule_import` with `data_json` that is not JSON | `is_error: true`, `data_json is not JSON: ...`. |
| Anything else (a bug, timekpr or D-Bus missing, a crash) | `is_error: true` with only `Error executing tool <name>`. The traceback goes to the server's stderr (over SSH, the client's MCP log). |

A rejected `policy_set` or `config_set` writes nothing and keeps no `.prev`.

## 6. The server's standing instructions

The server sends instructions to the client at connection time. They summarise the rules in [../../AGENTS.md](../../AGENTS.md):

1. The parent decides policy. Translate, preview, apply, verify, explain back; never change policy on your own judgement or from one ambiguous data point.
2. Preview, apply, verify in separate steps: `policy_validate` before `policy_set`, `enforce_plan` before `enforce_apply`, then `health`, `enforce_show` or `timeline`.
3. Never cut the child's session off by surprise; `force=true` only when the parent explicitly said so.
4. Never weaken monitoring on the child's request alone; relay it to the parent.
5. A one-off extension is a grant, never a policy edit.
6. Nothing returns a screenshot; during personal time app names and window titles are hidden on purpose.

They also say that `policy_set` applies nothing, and that every change is reported to the parent in plain words: what changed, what the preview said, how it was verified, how to undo it.

## 7. Undo

| Change | Undo |
|---|---|
| `policy_set` | The previous file is `/var/lib/familymon/policy.yaml.prev` (one level). Read it (for example `sudo cat` over SSH) and send it back with `policy_set`, which keeps the current file as the new `.prev`. Then `enforce_plan` and `enforce_apply` if enforced keys changed. |
| `config_set` | The previous override is `/etc/familymon/<name>.prev` (one level; none when there was no override before). Send it back with `config_set`, or delete the override by hand to fall back to the bundled file. |
| `grant` | `grant_revoke` (hours only; a playtime grant is a counter adjustment and has no revoke). Grants also expire at the end of the day. |
| `enforce_apply` | Restore the policy, then `enforce_plan` and `enforce_apply` again. |
| `schedule_import`, `schedule_refresh` | The previous schedule is `/var/lib/familymon/schedule.yaml.prev` (one level). Copy it back over `schedule.yaml` by hand (root), or re-import the previous export. |
| `games_forget` | The previous list is `/var/lib/familymon/known_games.json.prev`; copy it back by hand (root). A directory still installed is remembered again at the next scan anyway (within 30 minutes). |

## 8. Deliberately not offered

- No screenshots and no image of any kind. The timeline goes through the dashboard's masking, so during personal time the app and title are null.
- No raw database access and no SQL. The DB is read through the same read-only functions the CLI uses.
- No HTTP or other network transport; stdio over SSH only.
- No classification-policy edits beyond the config files `config_set` accepts (`schedule_extras.yaml`, `messaging_apps.yaml`, `game_apps.yaml`, `domains_starter.yaml`, `title_brands.yaml`). There is no tool for the category priority rule, for `classify --redo`, `retention`, `enforce web-block`, `enforce chargeback`, the dashboard, or the units.
- No signal the child can control that pauses or disables monitoring.
