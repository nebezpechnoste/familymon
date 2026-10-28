from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import click


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="DEBUG-level logging.")
def main(verbose: bool) -> None:
    """familymon control surface."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    # PIL is chatty at DEBUG — every pHash flushes dozens of stream-marker lines.
    logging.getLogger("PIL").setLevel(logging.INFO)


def _ops(fn, *args, **kwargs):
    """Call an ops function; a parent-facing OpsError becomes a click error.

    `UnsafeNow` (the live-session guard) passes through untouched: the
    commands that can hit it turn it into exit 3, which
    familymon-enforce.service and the docs rely on."""
    from familymon.ops import OpsError, UnsafeNow

    try:
        return fn(*args, **kwargs)
    except UnsafeNow:
        raise
    except OpsError as e:
        raise click.ClickException(str(e)) from None


def _refuse(e, *, hint: str) -> None:
    """Print an UnsafeNow refusal the way `enforce apply` always has, exit 3."""
    click.echo(f"refusing to {e.action} — it would cut off an active session:", err=True)
    for reason in e.reasons:
        click.echo(f"  - {reason}", err=True)
    click.echo(hint, err=True)
    raise SystemExit(3) from None


def _read_source(source: str) -> str:
    """A file path, or '-' for stdin."""
    import sys
    from pathlib import Path

    if source == "-":
        return sys.stdin.read()
    try:
        return Path(source).read_text()
    except OSError as e:
        raise click.ClickException(f"cannot read {source}: {e}") from None


_JSON_FLAG = click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")


@main.command()
@_JSON_FLAG
def status(as_json: bool) -> None:
    """Print runtime status: versions, state dir, what exists there."""
    from familymon.ops import status_summary, to_json

    r = status_summary()
    if as_json:
        click.echo(to_json(r))
        return
    pkg = f" (deb {r['package_version']})" if r["package_version"] else ""
    click.echo(f"version:         {r['version']}{pkg}")
    click.echo(f"state_dir:       {r['state_dir']}")
    click.echo(f"db_path:         {r['db']['path']} (exists={r['db']['exists']})")
    click.echo(
        f"screenshots_dir: {r['screenshots_dir']['path']} (exists={r['screenshots_dir']['exists']})"
    )
    click.echo(f"policy:          {r['policy']['path']} (exists={r['policy']['exists']})")
    click.echo(f"schedule:        {r['schedule']['path']} (exists={r['schedule']['exists']})")
    click.echo(f"monitored user:  {r['monitored_user'] or '(none: no policy user)'}")


@main.command("capture-once")
def capture_once() -> None:
    """Run a single capture tick and exit. For local sanity checks."""
    asyncio.run(_capture_once())


@main.command("capture-run")
def capture_run() -> None:
    """Run the capture loop inside your own session until SIGINT (dev runs).

    Production uses `capture-supervise` (root) instead.
    """
    asyncio.run(_capture_run())


@main.command("capture-supervise")
def capture_supervise() -> None:
    """Root capture loop for the monitored user (familymon-capture.service).

    Follows the user's graphical session through logind and runs one
    short-lived `capture-probe` as the user per tick; all state stays in the
    root state dir. See familymon/capture/supervisor.py.
    """
    if os.geteuid() != 0:
        raise click.ClickException(
            "capture-supervise runs as root (it launches the probe as the user)"
        )
    asyncio.run(_capture_supervise())


@main.command("capture-probe", hidden=True)
def capture_probe() -> None:
    """One in-session capture for the root supervisor: reads the request on
    stdin, prints the (already redacted) CaptureResult JSON on stdout."""
    import json
    import sys

    _make_undumpable()
    request = json.loads(sys.stdin.read() or "{}")
    result = asyncio.run(_probe_once(request))
    sys.stdout.buffer.write(result.model_dump_json().encode())
    sys.stdout.flush()


def _make_undumpable() -> None:
    """PR_SET_DUMPABLE=0: other processes of the same user can no longer open
    /proc/<pid>/fd (our stdout pipe) or attach to us."""
    import ctypes

    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)


async def _probe_once(request: dict):
    from pathlib import Path

    from familymon.capture.kwin_wayland import KWinWaylandBackend

    script = request.get("script")
    nonce = request.get("nonce")
    suppress = request.get("suppress")
    async with KWinWaylandBackend(
        script_path=Path(script) if script else None, nonce=nonce
    ) as backend:
        return await backend.snapshot(suppress_screenshot_reason=suppress)


@main.command("dashboard")
@click.option(
    "--bind",
    default=None,
    help="Interface to bind. Default 127.0.0.1; override with FAMILYMON_DASH_BIND "
    "for LAN exposure (e.g. 0.0.0.0).",
)
@click.option("--port", default=None, type=int, help="Override FAMILYMON_DASH_PORT.")
def dashboard_cmd(bind: str | None, port: int | None) -> None:
    """Start the FastAPI dashboard. Requires FAMILYMON_DASH_USER and FAMILYMON_DASH_PASS."""
    import uvicorn

    from familymon.dashboard.app import create_app

    app = create_app()
    host = bind or os.environ.get("FAMILYMON_DASH_BIND", "127.0.0.1")
    actual_port = port or int(os.environ.get("FAMILYMON_DASH_PORT", "8765"))
    click.echo(f"familymon dashboard binding {host}:{actual_port}")
    uvicorn.run(app, host=host, port=actual_port, log_level="info")


@main.command("heartbeat-once")
def heartbeat_once_cmd() -> None:
    """Run one capture-freshness check, log a heartbeat event, print status.

    Drive this from a systemd timer (1/min) so capture liveness is observed
    independently of the capture loop itself. Exit codes: 0=ok or idle (the
    monitored user isn't logged in), 1=stale, 2=dead, 3=never — non-zero
    marks the systemd unit failed, a secondary signal beyond the Event row.
    """
    from familymon.config import db_path
    from familymon.heartbeat import EXIT_CODES, check_capture_freshness, record_heartbeat
    from familymon.storage.session import make_engine, make_session_factory

    sessions = make_session_factory(make_engine(db_path()))
    session = asyncio.run(_monitored_session_label())
    status = check_capture_freshness(sessions, session=session)
    record_heartbeat(sessions, status)
    age = f"{status.age_seconds:.0f}s" if status.age_seconds is not None else "n/a"
    click.echo(f"heartbeat: {status.status} (last capture {age} ago; session {session})")
    raise SystemExit(EXIT_CODES[status.status])


async def _monitored_session_label() -> str:
    """The monitored user's graphical session as logind reports it (as root),
    or your own (dev): active / locked / inactive / none / unknown."""
    import pwd

    from familymon.capture.session_state import SessionState, session_label

    uid = os.getuid()
    if os.geteuid() == 0:
        from familymon.config import monitored_user

        try:
            uid = pwd.getpwnam(monitored_user()).pw_uid
        except (LookupError, KeyError):
            return "unknown"
    state = SessionState(uid=uid)
    try:
        return session_label(await state.status())
    finally:
        await state.close()


@main.command("health")
@_JSON_FLAG
def health_cmd(as_json: bool) -> None:
    """Print a data-health report. Exits non-zero on a privacy violation.

    Run this at the start of each dev iteration, before shipping changes.
    """
    from familymon.ops import health_summary, to_json

    h = health_summary()
    if as_json:
        click.echo(to_json(h))
        raise SystemExit(h["exit_code"])
    r = h["report"]

    def age(seconds: float | None) -> str:
        if seconds is None:
            return "n/a"
        if seconds < 90:
            return f"{int(seconds)}s"
        if seconds < 5400:
            return f"{int(seconds / 60)}m"
        return f"{seconds / 3600:.1f}h"

    click.echo(f"as of:            {r['now'][:19].replace('T', ' ')} (local)")
    click.echo(f"total captures:   {r['total_captures']}")
    click.echo(f"last 1h / 24h:    {r['captures_last_1h']} / {r['captures_last_24h']}")
    click.echo(f"last capture age: {age(r['last_capture_age_seconds'])}")
    click.echo(f"largest gap 24h:  {age(r['largest_gap_seconds_24h'])}")
    click.echo(f"classified 24h:   {r['classified_pct_24h']:.0f}%")
    if r["dedup_eligible_24h"] == 0:
        click.echo("dedup 24h:        n/a (no captures eligible — session idle/locked)")
    else:
        click.echo(
            f"dedup 24h:        {r['dedup_pct_24h']:.0f}% same-as-prev "
            f"({r['dedup_eligible_24h']} eligible)"
        )
    click.echo(f"msg full-skips24h:{r['messaging_skips_24h']}")
    click.echo(
        f"screenshots:      {r['screenshots_on_disk']} files, "
        f"{r['screenshot_disk_bytes'] / 1e6:.1f} MB"
    )
    if r["category_counts_24h"]:
        dist = ", ".join(
            f"{cat}={n}"
            for cat, n in sorted(r["category_counts_24h"].items(), key=lambda kv: -kv[1])
        )
        click.echo(f"categories 24h:   {dist}")
    hb = h["heartbeat"]
    click.echo(f"heartbeat:        {hb['status']} (session {hb['session'] or 'unknown'})")

    # Soft warnings.
    for w in h["warnings"]:
        click.echo(f"  ! {w}")

    # Hard failures: the privacy contract (exit 2) or a damaged DB (exit 4).
    for f in h["failures"]:
        click.echo(f"  !! {f}", err=True)
    if h["exit_code"]:
        raise SystemExit(h["exit_code"])
    click.echo("privacy check:    OK (no unredacted messaging screenshots on disk)")
    click.echo("db integrity:     ok")


@main.command("migrate-state")
@click.option("--user", "user", default=None, help="Monitored user (default: from policy.yaml).")
def migrate_state_cmd(user: str | None) -> None:
    """Move a monitored user's old state (captures, screenshots, schedule)
    from their home into the root state dir. Idempotent; the package's
    postinst runs it. Stop the user's old capture service first."""
    import pwd
    from pathlib import Path

    from familymon.config import monitored_user, state_dir, user_state_dir
    from familymon.storage.migrate import MigrationError, migrate_user_state

    if os.geteuid() != 0:
        raise click.ClickException("migrate-state runs as root")
    try:
        user = user or monitored_user()
        home = Path(pwd.getpwnam(user).pw_dir)
    except (LookupError, KeyError) as e:
        raise click.ClickException(f"no monitored user: {e}") from None
    try:
        report = migrate_user_state(user, user_state_dir(user), state_dir(), home=home)
    except (MigrationError, OSError) as e:
        raise click.ClickException(f"migration failed (safe to re-run): {e}") from None
    if report is None:
        click.echo(f"{user}: nothing to migrate")
        return
    click.echo(
        f"{user}: migrated {report.captures} capture(s), {report.screenshots_moved} "
        f"screenshot(s), carried {report.events_carried} root event(s); copied "
        f"{', '.join(report.copied) or 'nothing else'}"
        + (f"; kept root's {', '.join(report.kept_root)}" if report.kept_root else "")
        + (
            f"; appended {report.root_captures_carried} capture(s) root took meanwhile"
            if report.root_captures_carried
            else ""
        )
        + f". Old directory kept at {report.backup_dir}"
    )


@main.command("retention")
@click.option("--dry-run", is_flag=True, help="Report what would be deleted; change nothing.")
@click.option(
    "--days",
    type=int,
    default=None,
    help="Override retention.screenshots_days from policy.yaml (0 = never delete).",
)
def retention_cmd(dry_run: bool, days: int | None) -> None:
    """Delete screenshots older than the retention age (familymon-retention.timer, daily).

    Only the image files go, by the date of their day directory; the capture
    rows and classifications stay and their screenshot paths are cleared.
    The age is `retention.screenshots_days` in policy.yaml (default 90,
    0 = keep forever).
    """
    from familymon.config import db_path, policy_path, screenshots_dir
    from familymon.policy.enforcement import load_policy
    from familymon.storage.retention import prune_screenshots, screenshot_days
    from familymon.storage.session import make_engine, make_session_factory

    if days is None:
        policy = load_policy(policy_path()) if policy_path().exists() else {}
        try:
            days = screenshot_days(policy)
        except ValueError as e:
            raise click.ClickException(str(e)) from None
    if days <= 0:
        click.echo("retention off (screenshots_days is 0): nothing deleted")
        return
    sessions = make_session_factory(make_engine(db_path()))
    r = prune_screenshots(sessions, screenshots_dir(), keep_days=days, dry_run=dry_run)
    verb = "would delete" if dry_run else "deleted"
    span = f" ({r.days[0]} to {r.days[-1]})" if r.days else ""
    click.echo(
        f"{verb} {r.files} screenshot(s), {r.bytes / 1e6:.1f} MB, from {len(r.days)} "
        f"day(s){span}; keeping {days} days (days before {r.cutoff})"
    )
    if r.rows:
        cleared = "would clear" if dry_run else "cleared"
        click.echo(f"{cleared} the screenshot path on {r.rows} capture row(s)")


def _personal_time_window(now):
    """Today's personal-time window from policy.yaml and the active grants,
    or None. Raises on an unusable policy (PolicyError, OSError, YAML)."""
    from familymon.config import policy_path, state_dir
    from familymon.policy.personal_time import current_window

    path = policy_path()
    if not path.exists():
        return None
    return current_window(now, policy_path=path, grants_path=state_dir() / "grants.json")


@main.command("personal-time")
@click.option(
    "--day", default=None, help="Day YYYY-MM-DD to count hidden captures for (default: today)."
)
@_JSON_FLAG
def personal_time_cmd(day: str | None, as_json: bool) -> None:
    """Show today's personal-time window (policy.yaml `personal_time`) and
    what it covered: captures taken without a screenshot whose app and title
    the dashboard hides, and the game minutes among them. Read-only."""
    from familymon.ops import personal_time_summary, to_json

    r = _ops(personal_time_summary, day)
    if as_json:
        click.echo(to_json(r))
        return
    now = r["now"][11:16]
    if r["window"] is None:
        click.echo("personal time today: none")
    else:
        click.echo(f"personal time today: {r['window']['label']}")
        if r["active_now"]:
            click.echo(
                f"now {now}: in personal time (no screenshots; the dashboard hides "
                "app names and titles; categories and game time still count)"
            )
        else:
            click.echo(f"now {now}: outside personal time (full capture)")
    from familymon.config import db_path

    if not db_path().exists():
        return
    click.echo(
        f"hidden captures on {r['day']}: {r['hidden_captures']} "
        f"(games: {r['hidden_game_captures']}, about {r['hidden_game_minutes']} min)"
    )


@main.command("events")
@click.option("--day", default=None, help="Day YYYY-MM-DD (default: today).")
@click.option(
    "--kind",
    "kinds",
    multiple=True,
    help="Only these kinds (repeatable), e.g. capture_failed, capture_tamper.",
)
@_JSON_FLAG
def events_cmd(day: str | None, kinds: tuple[str, ...], as_json: bool) -> None:
    """List the day's operational events (heartbeats excluded unless asked):
    capture failures, tamper evidence, enforcement and grant audit rows."""
    import json

    from familymon.ops import events_on, to_json

    r = _ops(events_on, day, tuple(kinds))
    if as_json:
        click.echo(to_json(r))
        return
    for ev in r["events"]:
        click.echo(f"{ev['at'][11:19]}  {ev['kind']:20} {json.dumps(ev['data'], sort_keys=True)}")
    click.echo(f"{r['count']} event(s) on {r['day']}")


@main.group("schedule")
def schedule_grp() -> None:
    """View and import the school timetable."""


@schedule_grp.command("import")
@click.argument("source", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["kingsinterhigh"]),
    default="kingsinterhigh",
    help="Timetable export format.",
)
def schedule_import(source: str, fmt: str) -> None:
    """Import a timetable export into the schedule (overwrites the current one).

    For Kings InterHigh: save the JSON from
    GET /student/api/timetable/week and pass the file here.
    """
    import json
    from pathlib import Path

    from familymon.ops import schedule_import_data

    try:
        data = json.loads(Path(source).read_text())
    except ValueError as e:
        raise click.ClickException(f"{source} is not JSON: {e}") from None
    r = _ops(schedule_import_data, data, fmt)
    _echo_schedule_result("imported", r)


def _echo_schedule_result(verb: str, r: dict) -> None:
    imp = r["imported"]
    tz = f"timezone: {r['timezone']}; " if verb == "imported" else ""
    click.echo(
        f"{verb} {r['count']} block(s) -> {r['path']}"
        f" ({tz}{r['by_category'].get('school', 0)} school, {imp['extras']} extra, "
        f"excluded subjects: {', '.join(imp['excluded_subjects']) or 'none'}; "
        f"{imp['breaks']} break(s))"
    )
    _echo_breaks(r["breaks"])


def _echo_breaks(breaks: list[dict]) -> None:
    """One line per break, marking the one in progress today."""
    for br in breaks:
        now = " [in progress — no lessons today]" if br["in_progress"] else ""
        cats = ", ".join(br.get("categories") or ["school"])
        click.echo(f"  break: {br['label']} ({cats} blocks suspended){now}")


@schedule_grp.command("refresh")
def schedule_refresh() -> None:
    """Re-apply the parent extras to the current schedule, no export needed.

    For when /etc/familymon/schedule_extras.yaml changes (a school break, a
    new routine item) and the timetable export isn't at hand: the imported
    lessons are kept exactly as they are; exclusions, custom blocks and
    breaks are rebuilt from the extras file.
    """
    from familymon.ops import schedule_refresh as refresh

    _echo_schedule_result("refreshed", _ops(refresh))


@schedule_grp.command("show")
@_JSON_FLAG
def schedule_show(as_json: bool) -> None:
    """Print the current weekly schedule."""
    from familymon.ops import schedule_summary, to_json

    r = schedule_summary()
    if as_json:
        click.echo(to_json(r))
        return
    if not r["exists"]:
        click.echo(f"no schedule at {r['path']} — run 'familymonctl schedule import' first")
        return
    current_day = None
    for b in r["blocks"]:
        if b["day"] != current_day:
            current_day = b["day"]
            click.echo(f"\n{b['day']}")
        click.echo(
            f"  {b['start']}-{b['end']}  {b['category']:8} {b['subject']}"
            f"{'  [notify]' if b.get('notify') else ''}"
        )
    click.echo(f"\n{r['count']} block(s) total")
    if r["breaks"]:
        click.echo("")
        _echo_breaks(r["breaks"])


@schedule_grp.command("notify-tick")
@click.option("--test", "test_", is_flag=True, help="Send a sample notification now and exit.")
@click.option("--no-warn", is_flag=True, help="Skip the live lesson-conflict warning.")
@click.option(
    "--warn-cooldown",
    default=None,
    type=int,
    help="Minutes between repeated warnings for the same lesson+category (default 5).",
)
def schedule_notify_tick(test_: bool, no_warn: bool, warn_cooldown: int | None) -> None:
    """Pop a desktop notification for notify-flagged blocks starting now, and
    a warning when a distraction is seen during the lesson in progress.

    Runs every minute from familymon-notify.timer (root); notifications go
    into the monitored user's session. Per-day watermark files in the state
    dir prevent repeats.
    """
    from datetime import datetime

    from familymon.config import schedule_path

    send = _session_notifier()
    if test_:
        sent = send("familymon", "Schedule notifications are working.")
        click.echo("test notification sent" if sent else "no session to notify")
        return

    from familymon.policy.schedule import Schedule

    now = datetime.now()
    _notify_personal_time(now, send)
    path = schedule_path()
    if not path.exists():
        return
    schedule = Schedule.load(path)
    _notify_block_starts(schedule, now, send)
    if not no_warn:
        _warn_live_conflicts(schedule, now, warn_cooldown, send)


def _session_notifier():
    """notify(summary, body, icon=...) -> bool. As root: into the monitored
    user's session (notify.send_as). Otherwise (dev): into your own."""
    from familymon import notify

    if os.geteuid() != 0:

        def own(summary, body, **kw) -> bool:
            notify.send(summary, body, **kw)
            return True

        return own
    from familymon.config import monitored_user

    try:
        user = monitored_user()
    except LookupError as e:
        raise click.ClickException(str(e)) from None
    return lambda summary, body, **kw: notify.send_as(user, summary, body, **kw)


def _notify_personal_time(now, send) -> int:
    """Once a day, when personal time is in effect: tell the kid. Disclosed
    monitoring includes saying when it eases. Marked only when delivered, so
    a kid who logs in later in the window still gets it."""
    import json

    from familymon.config import state_dir

    try:
        window = _personal_time_window(now)
    except Exception:
        return 0  # an unusable policy is reported by `personal-time` and the capture log
    if window is None or not window.active(now):
        return 0
    mark = state_dir() / "personal_time_notified.json"
    try:
        seen = json.loads(mark.read_text())
    except (OSError, ValueError):
        seen = {}
    today = now.date().isoformat()
    if isinstance(seen, dict) and seen.get("day") == today:
        return 0
    body = (
        f"Until {window.end:%H:%M} familymon takes no screenshots, and the dashboard "
        "hides app names and window titles. Categories and game time still count."
    )
    if not send("Personal time", body, icon="user-away"):
        return 0
    mark.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mark.write_text(json.dumps({"day": today, "until": window.end.isoformat()}))
    click.echo("notified: personal time")
    return 1


def _notify_block_starts(schedule, now, send) -> int:
    """Notification at the start of notify-flagged blocks; one per block per day."""
    import json

    from familymon.config import state_dir

    due = schedule.starting_at(now)
    if not due:
        return 0
    mark = state_dir() / "notified.json"
    try:
        seen = json.loads(mark.read_text())
    except (OSError, ValueError):
        seen = {}
    today = now.date().isoformat()
    done = set(seen.get(today) or [])
    fired = 0
    for b in due:
        key = f"{b.start:%H:%M}|{b.subject}"
        if key in done:
            continue
        # Marked only when delivered: with nobody logged in, the next tick
        # (still inside the block's start minute) retries.
        if send(b.subject, f"{b.start:%H:%M}\u2013{b.end:%H:%M} \u2014 time for {b.subject}"):
            done.add(key)
            fired += 1
    if fired:
        mark.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        mark.write_text(json.dumps({today: sorted(done)}))
        click.echo(f"notified: {fired}")
    return fired


def _warn_live_conflicts(schedule, now, cooldown: int | None, send) -> int:
    """Live lesson-conflict warning \u2014 the "react" half of lesson-time
    enforcement (the Chrome policy block is the other; see policy/livewarn.py)."""
    from datetime import timedelta

    from familymon.config import db_path, state_dir
    from familymon.policy.livewarn import (
        DEFAULT_COOLDOWN_MINUTES,
        LOOKBACK_MINUTES,
        WarnStore,
        pending_warnings,
        recent_rows,
    )
    from familymon.storage.session import make_engine, make_session_factory

    # Cheap exit outside lessons: no DB open on most ticks.
    if schedule.block_at(now, category="school") is None or not db_path().exists():
        return 0
    # Judge by the active window on its own (a chat app in front of a school
    # page is still a chat app), with the same lookups the classifier uses.
    from familymon.analyze.heuristics import HeuristicClassifier
    from familymon.categorize.apps import AppCategoryLookup
    from familymon.categorize.brands import BrandTitleLookup
    from familymon.categorize.domains import DomainCategoryLookup

    brands = BrandTitleLookup.from_yaml()
    _add_schedule_brands(brands)
    classifier = HeuristicClassifier(
        AppCategoryLookup.from_dirs(), DomainCategoryLookup.from_yaml(), brands
    )
    rows = recent_rows(
        make_session_factory(make_engine(db_path())),
        now - timedelta(minutes=LOOKBACK_MINUTES),
        classifier,
    )
    store = WarnStore(state_dir() / "warned.json")
    due = pending_warnings(
        schedule,
        rows,
        now,
        store.last_warned(now.date()),
        DEFAULT_COOLDOWN_MINUTES if cooldown is None else cooldown,
    )
    warned = 0
    for w in due:
        summary, body = w.message()
        if not send(summary, body, icon="dialog-warning"):
            continue
        store.mark(w.key, now)
        warned += 1
        click.echo(f"warned: {w.category} during {w.block.subject}")
    if warned:
        store.save()
    return warned


@main.command("conflicts")
@click.option("--day", default=None, help="Day YYYY-MM-DD (default: today).")
def conflicts_cmd(day: str | None) -> None:
    """List leisure-during-lesson conflicts for a day."""
    from datetime import date, datetime, time, timedelta

    from sqlalchemy import select

    from familymon.config import capture_interval_seconds, db_path, schedule_path
    from familymon.policy.conflicts import find_conflicts
    from familymon.policy.schedule import Schedule
    from familymon.storage.models import Capture, Classification
    from familymon.storage.session import make_engine, make_session_factory

    if not schedule_path().exists():
        click.echo("no schedule imported — run 'familymonctl schedule import' first")
        return
    schedule = Schedule.load(schedule_path())

    target = date.fromisoformat(day) if day else date.today()
    start = datetime.combine(target, time.min)
    end = start + timedelta(days=1)

    sessions = make_session_factory(make_engine(db_path()))
    with sessions() as s:
        rows = s.execute(
            select(Capture.captured_at, Classification.category, Capture.active_title)
            .join(Classification, Classification.capture_id == Capture.id)
            .where(Capture.captured_at >= start, Capture.captured_at < end)
            .order_by(Capture.captured_at)
        ).all()

    episodes = find_conflicts(schedule, [(ts, cat, title) for ts, cat, title in rows])
    if not episodes:
        click.echo(f"{target}: no conflicts")
        return
    click.echo(f"{target}: {len(episodes)} conflict(s)")
    # Use the configured capture interval for the duration estimate, matching
    # the dashboard (a hardcoded 60s here disagreed with it under a non-default
    # FAMILYMON_INTERVAL).
    interval = capture_interval_seconds()
    for e in episodes:
        mins = e.seconds(interval) / 60.0
        label = f"{e.category} ({e.detail})" if e.detail else e.category
        click.echo(
            f"  {e.start:%H:%M}-{e.end:%H:%M}  {label:9} "
            f"during {e.block_subject}  (~{mins:.0f} min)"
        )


@main.group("enforce")
def enforce_grp() -> None:
    """Drive timekpr-next from the enforcement policy (policy.yaml).

    Runs on the machine timekpr enforces (needs root or `timekpr` group).
    Reads live state over D-Bus — timekpr 0.5.10's CLI misprints booleans.
    """


def _echo_changes(changes) -> None:
    for c in changes:
        click.echo(f"  {c['setting']}:")
        click.echo(f"    live:    {c['live']}")
        click.echo(f"    desired: {c['desired']}")


@enforce_grp.command("show")
@click.argument("user", required=False)
@_JSON_FLAG
def enforce_show(user: str | None, as_json: bool) -> None:
    """Print the live timekpr state for USER (default: the policy's user), read over D-Bus."""
    from familymon.ops import live_summary, to_json

    r = _ops(live_summary, user)
    if as_json:
        click.echo(to_json(r))
        return
    click.echo(f"user:              {r['user']}")
    click.echo(f"active sessions:   {r['active_sessions']}")
    click.echo(f"lockout:           {r['lockout']}")
    click.echo(f"allowed weekdays:  {';'.join(map(str, r['allowed_weekdays']))}")
    for day, span in r["allowed_hours"].items():
        click.echo(f"  {day[:3]}: {span}  limit={r['daily_limits'][day]}")
    pt = r["playtime"]
    click.echo(f"playtime enabled:  {pt['enabled']}")
    if pt["enabled"]:
        click.echo("playtime limits:   " + ";".join(pt["daily_limits"].values()))
        for a in pt["activities"]:
            click.echo(f"  activity: {a['mask']}  [{a['description']}]")


@enforce_grp.command("plan")
@_JSON_FLAG
def enforce_plan(as_json: bool) -> None:
    """Dry run: show what `apply` would change (no writes)."""
    from familymon.ops import plan_summary, to_json

    r = _ops(plan_summary)
    if as_json:
        click.echo(to_json(r))
        return
    if r["up_to_date"]:
        click.echo(f"{r['user']}: live state matches policy — nothing to do")
        return
    click.echo(f"{r['user']}: {r['count']} change(s) needed")
    _echo_changes(r["changes"])
    if not r["safe_now"]:
        click.echo("\n!! applying NOW would be unsafe:")
        for reason in r["reasons"]:
            click.echo(f"   - {reason}")
        click.echo("   (apply refuses without --force; or wait for logout)")


@enforce_grp.command("apply")
@click.option(
    "--force",
    is_flag=True,
    help="Apply even if it would cut off an active session right now.",
)
def enforce_apply(force: bool) -> None:
    """Apply the policy to the live timekpr daemon (minimal diff)."""
    from familymon.ops import UnsafeNow, apply_policy

    try:
        r = _ops(apply_policy, force=force)
    except UnsafeNow as e:
        _refuse(e, hint="re-run with --force to override, or wait for logout.")
    if r["up_to_date"]:
        click.echo(f"{r['user']}: live state matches policy — nothing to do")
        return
    _echo_changes(r["applied"])
    click.echo(f"applied {r['count']} change(s) to {r['user']}")


@enforce_grp.command("chargeback")
@click.option("--dry-run", is_flag=True, help="Report what would be charged; change nothing.")
def enforce_chargeback(dry_run: bool) -> None:
    """Charge browser-game minutes to the user's timekpr PlayTime budget.

    One reconcile tick (run from familymon-chargeback.timer every 5 min).
    Needs `games.charge_browser_games: true` in policy.yaml. Reads the
    capture DB read-only; the watermark lives next to it in the state dir.
    """
    from familymon.config import (
        capture_interval_seconds,
        db_path,
        policy_path,
        state_dir,
    )
    from familymon.policy.chargeback import (
        make_readonly_engine,
        record_charge_event,
        rewound_watermark,
        run_chargeback,
        sessions_for,
    )
    from familymon.policy.enforcement import load_policy
    from familymon.policy.timekpr_admin import LiveTimekpr
    from familymon.storage.session import make_engine, make_session_factory

    if not policy_path().exists():
        raise click.ClickException(f"no policy at {policy_path()}")
    policy = load_policy(policy_path())
    user = policy.get("user")
    games = policy.get("games") or {}
    if not (isinstance(user, str) and games.get("enabled") and games.get("charge_browser_games")):
        click.echo("chargeback disabled (needs games.enabled and games.charge_browser_games)")
        return

    when = str(games.get("charge_browser_games_when", "visible")).strip().lower()
    if when not in ("visible", "focused"):
        raise click.ClickException(
            f"games.charge_browser_games_when must be 'visible' or 'focused', got {when!r}"
        )
    focused_pred = _focused_title_is_game() if when == "focused" else None

    if not db_path().exists():
        click.echo(f"{user}: no capture DB at {db_path()} yet — nothing to charge")
        return
    user_sessions = sessions_for(make_readonly_engine(db_path()))
    state_path = state_dir() / "playtime_charges.json"

    if dry_run:
        from datetime import date

        from familymon.policy.chargeback import ChargeState, find_chargeable_captures

        st = ChargeState.load(state_path, user, date.today())
        rewound_watermark(st, user_sessions)
        rows = find_chargeable_captures(
            user_sessions,
            since_id=st.last_capture_id,
            today=date.today(),
            focused_title_is_game=focused_pred,
        )
        click.echo(
            f"{user}: [{when}] {len(rows)} uncharged browser-game capture(s) "
            f"(~{int(len(rows) * capture_interval_seconds())}s); charged today so far "
            f"{st.charged_seconds_today}s"
        )
        return

    transport = LiveTimekpr()
    result = run_chargeback(
        user,
        user_sessions,
        state_path,
        lambda u, secs: transport.adjust_playtime_left(u, -secs),
        interval_seconds=capture_interval_seconds(),
        focused_title_is_game=focused_pred,
    )
    if result.captures:
        record_charge_event(make_session_factory(make_engine(db_path())), user, result)
        click.echo(
            f"{user}: charged {result.seconds}s for {result.captures} browser-game "
            f"capture(s){' (capped; more next tick)' if result.capped else ''}"
        )
    else:
        click.echo(f"{user}: nothing new to charge")


def _focused_title_is_game():
    """Predicate for the 'focused' charge rule: does this browser title itself
    classify as games (domain-in-title, brand, or schedule-aware brands)?"""
    from familymon.analyze.categories import Category
    from familymon.analyze.heuristics import _domain_candidates_from_title
    from familymon.categorize.brands import BrandTitleLookup
    from familymon.categorize.domains import DomainCategoryLookup

    domains = DomainCategoryLookup.from_yaml()
    brands = BrandTitleLookup.from_yaml()
    _add_schedule_brands(brands)

    def pred(title: str | None) -> bool:
        for cand in _domain_candidates_from_title(title):
            cat = domains.lookup(cand)
            if cat is not None:
                return cat is Category.GAMES
        hit = brands.lookup(title)
        return hit is not None and hit[0] is Category.GAMES

    return pred


@enforce_grp.command("grant")
@click.argument("user")
@click.option(
    "--until", "until_", default=None, help="Extend today's allowed hours until HH:MM (inclusive)."
)
@click.option("--from", "from_", default=None, help="Grant start HH:MM (default: now).")
@click.option(
    "--playtime", default=None, help="Extra game minutes today, e.g. 20m or 1h (counter adjust)."
)
@click.option("--note", default="", help="Why (kept with the grant).")
def enforce_grant(
    user: str, until_: str | None, from_: str | None, playtime: str | None, note: str
) -> None:
    """One-off grant for today, applied immediately and honored by the reconciler.

    --until widens today's allowed-hours window (expires on its own, never
    reverted by familymon-enforce.timer); --playtime adds game minutes.
    """
    from familymon.ops import UnsafeNow, grant

    try:
        r = _ops(grant, user, until=until_, from_=from_, playtime=playtime, note=note)
    except UnsafeNow as e:
        _refuse(
            e,
            hint="the grant was not stored. Apply the pending policy first "
            "(enforce plan, then enforce apply) or wait for logout.",
        )
    if "hours" in r:
        h = r["hours"]
        click.echo(
            f"{user}: allowed until {h['end']} today ({h['start']}-{h['end']}); "
            f"{len(h['timekpr_changes'])} timekpr change(s) applied"
        )
        if h["deferred"]:
            click.echo(
                "pending policy changes NOT applied (would cut the active session): "
                + ", ".join(h["deferred"])
                + " — enforce plan, then enforce apply"
            )
    if "playtime_seconds" in r:
        click.echo(f"{user}: +{r['playtime_seconds']}s game time today")


@enforce_grp.command("grants")
@_JSON_FLAG
def enforce_grants(as_json: bool) -> None:
    """List today's active one-off grants."""
    from familymon.ops import grants_active, to_json

    r = grants_active()
    if as_json:
        click.echo(to_json(r))
        return
    if not r["grants"]:
        click.echo("no active grants")
        return
    for g in r["grants"]:
        click.echo(f"  {g['user']}: {g['day']} {g['start']}-{g['end']}  {g['note']}".rstrip())


@enforce_grp.command("revoke")
@click.argument("user")
def enforce_revoke(user: str) -> None:
    """Drop today's hour grants for USER and converge back to policy (guarded)."""
    from familymon.ops import revoke_grants

    r = _ops(revoke_grants, user)
    if r["deferred"]:
        click.echo(
            f"revoked {r['revoked']} grant(s); timekpr NOT narrowed now (would cut the active "
            "session) — the reconciler converges once safe"
        )
        return
    click.echo(f"revoked {r['revoked']} grant(s); {len(r['applied'])} timekpr change(s) applied")


@enforce_grp.command("games")
@click.option(
    "--forget",
    "forget",
    multiple=True,
    metavar="DIR",
    help="Stop treating this remembered game dir as a game (repeatable).",
)
@_JSON_FLAG
def enforce_games(forget: tuple[str, ...], as_json: bool) -> None:
    """List the game dirs familymon budgets (remembered + installed now).

    A game stays on the list after it disappears from the Steam library, so
    hiding a game's files doesn't take it off the PlayTime budget. Only the
    parent removes one, with --forget. The next `enforce apply` (hourly)
    updates timekpr's masks.
    """
    from familymon.ops import games_forget, games_known, to_json

    if forget:
        _ops(games_forget, forget)
        click.echo(
            f"forgot {len(forget)} game dir(s); one still installed is re-added on the next scan"
        )
        return
    r = games_known()
    if as_json:
        click.echo(to_json(r))
        return
    for g in r["games"]:
        click.echo(f"  {g['dir']}{'' if g['installed'] else '  (not installed now)'}")


@enforce_grp.command("web-block")
@click.option("--dry-run", is_flag=True, help="Show the desired Chrome policy; write nothing.")
@click.option(
    "--schedule",
    "schedule_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Schedule YAML to evaluate (default: the imported schedule).",
)
def enforce_web_block(dry_run: bool, schedule_file: str | None) -> None:
    """Reconcile Chrome's managed policy to lesson-time web blocking.

    One tick (familymon-webblock.timer, every minute): while a school block
    is in progress, URLBlocklist = the configured distraction domains;
    otherwise the key is dropped. Needs `web_block.enabled: true` in
    policy.yaml. Idempotent — writes only on change. Root: the policy dir is
    /etc/opt/chrome/policies/managed.
    """
    import json
    from datetime import datetime
    from pathlib import Path

    from familymon.config import config_file, db_path, policy_path, schedule_path
    from familymon.policy.crx import load_extension_ref
    from familymon.policy.enforcement import PolicyError, load_policy
    from familymon.policy.schedule import Schedule
    from familymon.policy.webblock import (
        CHROME_POLICY_DIR,
        POLICY_FILE_NAME,
        describe,
        describe_extension,
        domains_preview,
        load_domain_table,
        parse_web_block,
        plan_web_block,
        read_document,
        reconcile,
    )

    if not policy_path().exists():
        raise click.ClickException(f"no policy at {policy_path()}")
    policy = load_policy(policy_path())
    user = policy.get("user")
    if not isinstance(user, str):
        raise click.ClickException("policy needs a 'user'")
    try:
        cfg = parse_web_block(policy)
    except PolicyError as e:
        raise click.ClickException(str(e)) from None

    schedule = None
    sched_path = Path(schedule_file) if schedule_file else schedule_path()
    if sched_path.exists():
        schedule = Schedule.load(sched_path)
    elif cfg.enabled:
        click.echo(f"warning: no schedule at {sched_path} — nothing to block until one is imported")

    table = load_domain_table(config_file("domains_starter.yaml")) if cfg.enabled else {}
    now = datetime.now()
    extension = load_extension_ref() if cfg.enabled else None
    plan = plan_web_block(cfg, schedule, now, table, extension=extension)
    path = CHROME_POLICY_DIR / POLICY_FILE_NAME

    # The sweep always runs while enabled: even with no other browser or app
    # forbidden, a Chrome started with extensions disabled must be closed.
    sweep = cfg.enabled

    if dry_run:
        click.echo(describe(plan))
        br = schedule.break_on(now) if schedule is not None else None
        if br is not None:
            click.echo(f"  school break: {br.label} — {', '.join(br.categories)} blocks suspended")
        if plan.document is not None:
            click.echo(f"  {describe_extension(extension)}")
            click.echo(f"  private windows (incognito/guest): {cfg.block_private}")
            click.echo(f"  file: {path} ({'exists' if path.exists() else 'absent'})")
            click.echo(f"  block set ({len(plan.blocked)}): {domains_preview(plan.blocked)}")
            click.echo(f"  desired now: {json.dumps(plan.document)}")
            same = read_document(path) == plan.document
            click.echo(f"  change needed: {'no' if same else 'yes'}")
        if sweep:
            would = _sweep_forbidden(user, cfg, kill=False)
            names = ", ".join(f"{c.name} (pid {c.pid})" for c in would) or "none"
            click.echo(f"  forbidden apps running now (closed only during a lesson): {names}")
        return

    try:
        changed = reconcile(path, plan.document)
    except PermissionError as e:
        raise click.ClickException(f"cannot write {path} — run as root ({e})") from None

    closed = _sweep_forbidden(user, cfg, kill=True) if sweep and plan.block else []

    if not changed and not closed:
        click.echo(f"converged: {describe(plan)}")
        return
    from familymon.storage.models import Event
    from familymon.storage.session import make_engine, make_session_factory

    sessions = make_session_factory(make_engine(db_path()))
    if changed:
        _audit(
            sessions,
            Event,
            "web_block",
            {
                "user": user,
                "active": plan.block is not None,
                "subject": plan.block.subject if plan.block else None,
                "domains": len(plan.blocked) if plan.block else 0,
            },
        )
        click.echo(f"updated: {describe(plan)}")
    if closed:
        from familymon.notify import send_as

        names = sorted({c.name for c in closed})
        _audit(
            sessions,
            Event,
            "lesson_close",
            {
                "user": user,
                "subject": plan.block.subject,
                "apps": names,
                "pids": [c.pid for c in closed],
                "forced": [c.pid for c in closed if c.forced],
            },
        )
        b = plan.block
        send_as(
            user,
            f"{b.subject} — lesson in progress",
            f"{', '.join(n.capitalize() for n in names)} isn't available during "
            f"{b.subject} (until {b.end:%H:%M}). Use Chrome for lessons.",
            icon="dialog-warning",
        )
        click.echo(f"closed during {b.subject}: {', '.join(names)} ({len(closed)} process(es))")


def _sweep_forbidden(user: str, cfg, *, kill: bool):
    """Processes of the policy user that are forbidden during a lesson —
    closed (kill=True) or just listed."""
    import os
    import pwd

    from familymon.policy.webblock import close_forbidden, compile_masks, scan_processes

    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        raise click.ClickException(f"policy user {user!r} does not exist") from None
    if uid == 0:
        raise click.ClickException("refusing to sweep processes of uid 0")
    if kill and os.geteuid() != 0:
        # A dev-machine run would sweep the developer's own browser.
        raise click.ClickException("closing processes needs root")
    return close_forbidden(scan_processes(uid), cfg, compile_masks(cfg), kill=kill)


def _audit(root_sessions, Event, kind: str, data: dict) -> None:
    from datetime import datetime

    with root_sessions() as s:
        s.add(Event(at=datetime.now(), kind=kind, data=data))
        s.commit()


@main.command("classify")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap the number of rows processed (useful for first-run sanity).",
)
@click.option(
    "--redo",
    is_flag=True,
    help="Discard all heuristic-sourced classifications and re-run the heuristics "
    "over the whole history (after a classification-policy change). VLM / "
    "bootstrap labels are kept.",
)
def classify_cmd(limit: int | None, redo: bool) -> None:
    """Classify all captures that don't yet have a Classification row.

    Heuristic-only for phase 1: app lookup via .desktop scan, domain lookup
    via bundled YAML, fallback UNKNOWN. Idempotent — skips rows that already
    have a Classification.
    """
    from familymon.analyze.classifier_runner import (
        classify_unclassified,
        reclassify_heuristic,
    )
    from familymon.analyze.heuristics import HeuristicClassifier
    from familymon.categorize.apps import AppCategoryLookup
    from familymon.categorize.brands import BrandTitleLookup
    from familymon.categorize.domains import DomainCategoryLookup
    from familymon.config import db_path
    from familymon.storage.session import make_engine, make_session_factory

    apps = AppCategoryLookup.from_dirs()
    domains = DomainCategoryLookup.from_yaml()
    brands = BrandTitleLookup.from_yaml()
    # The imported timetable's group codes ("10L CYCLE TEST AC-26/27") are how
    # the school's LMS titles its pages — add them as school title keywords so
    # those pages don't fall through to browsing_other. Refreshes per import.
    _add_schedule_brands(brands)
    classifier = HeuristicClassifier(apps, domains, brands)

    engine = make_engine(db_path())
    sessions = make_session_factory(engine)

    if redo:
        count = reclassify_heuristic(classifier, sessions)
        verb = "re-classified"
    else:
        count = classify_unclassified(classifier, sessions, limit=limit)
        verb = "classified"
    click.echo(
        f"{verb} {count} capture(s) "
        f"(apps={len(apps)} keys, domains={len(domains)}, brands={len(brands)})"
    )


def _add_schedule_brands(brands) -> int:
    """Append the schedule's group codes as `school` title brands. Returns how
    many were added (0 when no schedule is imported)."""
    from familymon.analyze.categories import Category
    from familymon.config import schedule_path
    from familymon.policy.schedule import Schedule

    path = schedule_path()
    if not path.exists():
        return 0
    keywords = Schedule.load(path).title_keywords()
    for kw in keywords:
        brands.add(kw, Category.SCHOOL)
    return len(keywords)


async def _capture_once() -> None:
    from familymon.analyze.dedup import PHashRingBuffer
    from familymon.capture.kwin_wayland import KWinWaylandBackend
    from familymon.capture.loop import run_one_tick
    from familymon.capture.persist import CapturePersistence
    from familymon.config import db_path, screenshots_dir
    from familymon.storage.session import make_engine, make_session_factory

    engine = make_engine(db_path())
    persistence = CapturePersistence(make_session_factory(engine), screenshots_dir())
    phash_buf = PHashRingBuffer()

    async with KWinWaylandBackend() as backend:
        outcome = await run_one_tick(backend, persistence, phash_buf=phash_buf)

    click.echo(
        f"id={outcome.capture_id} active={outcome.active_wm_class!r} "
        f"shot={outcome.screenshot_bytes_len} skipped={outcome.skipped_reason}"
    )


async def _capture_run() -> None:
    from familymon.capture.games import GameLibrary
    from familymon.capture.kwin_wayland import KWinWaylandBackend
    from familymon.capture.loop import run_capture_loop
    from familymon.capture.persist import CapturePersistence
    from familymon.capture.session_state import SessionState
    from familymon.config import capture_interval_seconds, db_path, screenshots_dir
    from familymon.storage.session import make_engine, make_session_factory

    engine = make_engine(db_path())
    persistence = CapturePersistence(make_session_factory(engine), screenshots_dir())
    session_state = SessionState()
    # Scanned once at startup; a service restart (or reboot) re-scans. Periodic
    # in-loop refresh for games installed mid-session is a tracked follow-up.
    gamelib = GameLibrary.from_system()

    try:
        async with KWinWaylandBackend() as backend:
            await run_capture_loop(
                backend,
                persistence,
                interval=capture_interval_seconds(),
                session_state=session_state,
                gamelib=gamelib,
            )
    finally:
        await session_state.close()


async def _capture_supervise() -> None:
    from familymon.capture.persist import CapturePersistence
    from familymon.capture.supervisor import run_supervisor
    from familymon.config import (
        capture_interval_seconds,
        db_path,
        monitored_user,
        screenshots_dir,
    )
    from familymon.storage.session import make_engine, make_session_factory

    try:
        user = monitored_user()
    except LookupError as e:
        raise click.ClickException(str(e)) from None
    sessions = make_session_factory(make_engine(db_path()))
    persistence = CapturePersistence(sessions, screenshots_dir())
    click.echo(f"familymon capture supervisor: watching {user}")
    await run_supervisor(
        user,
        persistence=persistence,
        sessions=sessions,
        interval=capture_interval_seconds(),
        load_gamelib=lambda: _game_library(user),
        personal_time=_personal_time_window,
    )


def _game_library(user: str):
    """The monitored user's games (ops.game_library), as a click error on a bad user."""
    from familymon.ops import game_library

    return _ops(game_library, user)


# ---- policy and config files --------------------------------------------------


@main.group("policy")
def policy_grp() -> None:
    """Read, validate and replace policy.yaml in place (no file copying)."""


@policy_grp.command("get")
@_JSON_FLAG
def policy_get(as_json: bool) -> None:
    """Print policy.yaml as it is (comments included); --json adds its parsed meaning."""
    from familymon.ops import policy_read, to_json

    r = policy_read()
    if not r["exists"]:
        raise click.ClickException(f"no policy at {r['path']}")
    if as_json:
        click.echo(to_json(r))
        return
    click.echo(r["text"], nl=False)


@policy_grp.command("validate")
@click.argument("source", required=False)
@_JSON_FLAG
def policy_validate(source: str | None, as_json: bool) -> None:
    """Check a policy document (a file, '-' for stdin, default: the live
    policy) against every parser that reads policy.yaml. Exit 1 if invalid."""
    from familymon.config import policy_path
    from familymon.ops import to_json, validate_policy

    text = _read_source(source) if source else _read_source(str(policy_path()))
    r = validate_policy(text)
    if as_json:
        click.echo(to_json(r))
    elif r["ok"]:
        s = r["summary"]
        click.echo(f"policy OK: user {s['user']}, lockout {s['lockout']}")
        for day, span in s["allowed_hours"].items():
            click.echo(f"  {day[:3]}: {span}  limit={s['daily_limits'][day]}")
        games = s["games"]
        click.echo(
            f"  games: {'on' if games['enabled'] else 'off'}; browser games charged: "
            f"{games['charge_browser_games']} ({games['charge_browser_games_when']})"
        )
        click.echo(f"  web_block: {'on' if s['web_block']['enabled'] else 'off'}")
        pt = ", ".join(
            f"{day[:3]} {rule['from']}-{rule['until']}"
            for day, rule in s["personal_time"].items()
            if rule
        )
        click.echo(f"  personal_time: {pt or 'none'}")
        click.echo(f"  retention: {s['retention_screenshot_days']} day(s) of screenshots")
    else:
        for err in r["errors"]:
            click.echo(f"  ! {err}", err=True)
    if not r["ok"]:
        raise SystemExit(1)


@policy_grp.command("set")
@click.argument("source")
def policy_set(source: str) -> None:
    """Replace policy.yaml with a complete document (a file, or '-' for
    stdin) after validating it. The previous file is kept as
    policy.yaml.prev. Nothing is applied: the output says what makes each
    change take effect (usually `enforce plan`, then `enforce apply`)."""
    from familymon.ops import policy_write

    r = _ops(policy_write, _read_source(source))
    prev = f" (previous kept at {r['previous_saved_to']})" if r["previous_saved_to"] else ""
    click.echo(f"wrote {r['path']}{prev}")
    if not r["changed_keys"]:
        click.echo("  no top-level key changed")
    for key, effect in r["takes_effect"].items():
        click.echo(f"  {key}: {effect}")


@main.group("config")
def config_grp() -> None:
    """Read and replace the parent-editable config files (/etc/familymon overrides)."""


@config_grp.command("list")
def config_list() -> None:
    """The config files this can manage, and whether an override exists."""
    from familymon.ops import CONFIG_FILES, config_read

    for name in sorted(CONFIG_FILES):
        r = config_read(name)
        click.echo(f"  {name:22} {'override' if r['overridden'] else 'bundled '}  {r['path']}")


@config_grp.command("get")
@click.argument("name")
def config_get(name: str) -> None:
    """Print a config file as resolved now (override if present, else bundled)."""
    from familymon.ops import config_read

    click.echo(_ops(config_read, name)["text"], nl=False)


@config_grp.command("set")
@click.argument("name")
@click.argument("source")
def config_set(name: str, source: str) -> None:
    """Write a config override (a file, or '-' for stdin) after validating
    it with the loader that will read it. The previous override is kept as
    <name>.prev."""
    from familymon.ops import config_write

    r = _ops(config_write, name, _read_source(source))
    prev = f" (previous kept at {r['previous_saved_to']})" if r["previous_saved_to"] else ""
    click.echo(f"wrote {r['path']}{prev}")
    click.echo(f"  takes effect: {r['takes_effect']}")


# ---- MCP -------------------------------------------------------------------------


@main.command("mcp")
def mcp_cmd() -> None:
    """Serve the operator surface as MCP tools on stdin/stdout.

    For the parent's AI agent, launched over SSH (`ssh kid-laptop sudo -n
    familymonctl mcp`): the same operations as the CLI, as typed tools.
    Nothing listens on the network. Runs as root in production.
    """
    try:
        from familymon.mcp_server import run_stdio
    except ImportError as e:
        raise click.ClickException(
            f"the MCP server needs the 'mcp' extra (uv sync --extra mcp): {e}"
        ) from None
    run_stdio()


# ---- extension ----------------------------------------------------------------


@main.group("extension")
def extension_grp() -> None:
    """The lesson-block Chrome extension (closes already-open blocked tabs).

    Built on the dev machine by scripts/build-deb.sh and shipped inside the
    deb; the web-block tick force-installs it through Chrome policy.
    """


@extension_grp.command("build")
@click.option(
    "--key",
    "key_file",
    required=True,
    type=click.Path(dir_okay=False),
    help="RSA private key (PEM) that signs the CRX; created if missing.",
)
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(file_okay=False),
    help="Directory to write familymon.crx, update.xml, id, version into.",
)
@click.option("--version", "version_", default=None, help="Extension version (default: from now).")
@click.option(
    "--src",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Extension source directory (default: the bundled one).",
)
@click.option(
    "--deployed-dir",
    default=None,
    help="Where the deb installs the output; baked into update.xml as file:// URLs.",
)
def extension_build(
    key_file: str, out_dir: str, version_: str | None, src: str | None, deployed_dir: str | None
) -> None:
    """Sign and package the extension as a CRX3 plus its update manifest."""
    from datetime import datetime
    from pathlib import Path

    from familymon.policy.crx import (
        EXTENSION_DIR,
        EXTENSION_SRC,
        build_extension,
        version_from_timestamp,
    )

    key = Path(key_file)
    if not key.exists():
        click.echo(f"generating a new extension signing key at {key}")
    ref = build_extension(
        key,
        Path(out_dir),
        version_ or version_from_timestamp(datetime.now()),
        src=Path(src) if src else EXTENSION_SRC,
        deployed_dir=Path(deployed_dir) if deployed_dir else EXTENSION_DIR,
    )
    click.echo(f"extension {ref.id} v{ref.version} -> {out_dir} (update url {ref.update_url})")


if __name__ == "__main__":
    main()
