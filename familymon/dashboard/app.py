"""FastAPI dashboard — per-day activity timeline.

Renders a day's timeline of classified captures, a capture-liveness heartbeat
banner, and any lesson-time conflicts.

Auth is HTTP Basic; credentials come from FAMILYMON_DASH_USER /
FAMILYMON_DASH_PASS env vars. The app refuses to start without them.

Binding defaults to 127.0.0.1; set FAMILYMON_DASH_BIND=0.0.0.0 (or a specific
LAN address) to expose on the LAN. We never bind anywhere by accident — the
CLI surfaces the chosen bind in its startup log so misconfiguration is
visible.

Routes:
  GET /                — today's timeline (HTML)
  GET /d/{YYYY-MM-DD}  — a specific day's timeline (HTML)
  GET /api/captures    — captures + classifications for the day (JSON)
  GET /api/stats       — seconds-per-category aggregation (JSON)
"""

from __future__ import annotations

import os
import secrets
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from familymon.config import capture_interval_seconds, db_path
from familymon.storage.models import Capture, Classification
from familymon.storage.session import make_readonly_engine, make_session_factory

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _make_auth_dependency(user: str, pwd: str) -> Callable[..., str]:
    security = HTTPBasic(realm="familymon")

    # Use the default-value form (`= Depends(security)`) rather than
    # Annotated[..., Depends(security)] — under Python 3.14's deferred
    # annotation evaluation (PEP 649), the closure-captured `security` /
    # `auth` symbols aren't resolved at type-hint extraction time, and
    # FastAPI then mistakes the parameter for a query string instead of a
    # dependency. The default-value form is captured at function-definition
    # time and works regardless.
    def check(creds: HTTPBasicCredentials = Depends(security)) -> str:
        ok_user = secrets.compare_digest(creds.username.encode(), user.encode())
        ok_pass = secrets.compare_digest(creds.password.encode(), pwd.encode())
        if not (ok_user and ok_pass):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": 'Basic realm="familymon"'},
            )
        return creds.username

    return check


def _day_bounds(target: date) -> tuple[datetime, datetime]:
    # Local-naive, matching how the capture loop stores timestamps (SQLite
    # strips tz offsets on write — see health.py). Labeling these bounds UTC
    # made the day window drift from the local day and only worked by accident
    # of ISO-string comparison; tests seeded via now(UTC) then failed between
    # 00:00 and 02:00 CEST when the UTC date is still "yesterday".
    start = datetime.combine(target, time.min)
    return start, start + timedelta(days=1)


def _load_captures(sessions: sessionmaker, target: date) -> list[dict[str, Any]]:
    start, end = _day_bounds(target)
    with sessions() as session:
        rows = session.execute(
            select(Capture, Classification)
            .outerjoin(Classification, Classification.capture_id == Capture.id)
            .where(Capture.captured_at >= start, Capture.captured_at < end)
            .order_by(Capture.captured_at)
        ).all()

    return [
        {
            "id": cap.id,
            "captured_at": cap.captured_at.isoformat(),
            "active_wm_class": cap.active_wm_class,
            "active_title": cap.active_title,
            "same_as_prev": cap.same_as_prev,
            "screenshot_skipped_reason": cap.screenshot_skipped_reason,
            "screenshot_on_disk": cap.screenshot_path is not None,
            "category": cls.category if cls else None,
            "confidence": cls.confidence if cls else None,
            "source": cls.source if cls else None,
            "evidence": cls.evidence_text if cls else None,
            "present_categories": (cls.present_categories if cls else None) or [],
        }
        for cap, cls in rows
    ]


def _block_duration_seconds(block: dict[str, Any], default_interval: float) -> float:
    """Compute a block's elapsed seconds from its own timestamps.

    For a block of N captures, the mean inter-capture interval is
    (end - start) / (N - 1). Total duration is N × that mean — which equals
    (end - start) + one tick at the end, treating each capture as the start of
    one tick of activity. Single-capture blocks fall back to the configured
    interval since we have no in-block evidence of the actual cadence.

    This avoids the bug where the dashboard assumes 60 s/capture regardless of
    what FAMILYMON_INTERVAL the capture run actually used.
    """
    count = block["count"]
    if count <= 1:
        return float(default_interval)
    start = datetime.fromisoformat(block["start"])
    end = datetime.fromisoformat(block["end"])
    span = (end - start).total_seconds()
    avg_interval = span / (count - 1)
    return count * avg_interval


def _build_blocks(
    captures: list[dict[str, Any]],
    default_interval: float,
) -> list[dict[str, Any]]:
    """Collapse contiguous (category, wm_class) runs into single timeline blocks."""
    from familymon.policy.conflicts import streaming_brand_in_title

    blocks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for c in captures:
        cat = c["category"] or "unknown"
        # A streaming brand seen on *any* capture in the block — the first title
        # alone would miss a class-stream → YouTube switch within one browser
        # window (same category+wm_class, so the captures merge).
        brand = streaming_brand_in_title(c["active_title"])
        if (
            cur
            and cur["category"] == cat
            and cur["wm_class"] == c["active_wm_class"]
        ):
            cur["count"] += 1
            cur["end"] = c["captured_at"]
            cur["present"].update(c["present_categories"])
            cur["stream_brand"] = cur["stream_brand"] or brand
        else:
            if cur is not None:
                blocks.append(cur)
            cur = {
                "category": cat,
                "wm_class": c["active_wm_class"],
                "title": c["active_title"],
                "skipped_reason": c["screenshot_skipped_reason"],
                "start": c["captured_at"],
                "end": c["captured_at"],
                "count": 1,
                "present": set(c["present_categories"]),
                "stream_brand": brand,
            }
    if cur is not None:
        blocks.append(cur)
    for b in blocks:
        b["duration_seconds"] = _block_duration_seconds(b, default_interval)
        # "Also present" = other categories seen on screen during the block,
        # besides the primary. Surfaces the multi-window reality at a glance.
        b["also"] = sorted(b.pop("present") - {b["category"]})
    return blocks


def _compute_stats_from_blocks(
    captures: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    by_cat: dict[str, float] = defaultdict(float)
    for b in blocks:
        by_cat[b["category"]] += b["duration_seconds"]
    return {
        "total_captures": len(captures),
        "seconds_per_category": dict(by_cat),
    }


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h, rem = divmod(s, 3600)
    return f"{h}h{rem // 60:02d}m"


def _fmt_clock(iso_ts: str) -> str:
    """Render the HH:MM:SS portion of an ISO-8601 timestamp."""
    return iso_ts[11:19]


def create_app(
    *,
    session_factory: sessionmaker | None = None,
    user: str | None = None,
    pwd: str | None = None,
) -> FastAPI:
    user = user or os.environ.get("FAMILYMON_DASH_USER")
    pwd = pwd or os.environ.get("FAMILYMON_DASH_PASS")
    if not user or not pwd:
        raise RuntimeError(
            "Dashboard requires FAMILYMON_DASH_USER and FAMILYMON_DASH_PASS "
            "env vars (or explicit user/pwd args)."
        )

    if session_factory is None:
        # Read-only: the dashboard faces the network and never needs to
        # write; in production it runs as the unprivileged `familymon` user
        # with group read access only (storage/perms.py).
        session_factory = make_session_factory(make_readonly_engine(db_path()))

    app = FastAPI(title="familymon", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    auth = _make_auth_dependency(user, pwd)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, _authed: str = Depends(auth)) -> HTMLResponse:
        return _render_timeline(request, templates, session_factory, date.today())

    @app.get("/d/{day}", response_class=HTMLResponse)
    def by_day(
        request: Request,
        day: str,
        _authed: str = Depends(auth),
    ) -> HTMLResponse:
        try:
            target = date.fromisoformat(day)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="invalid date") from e
        return _render_timeline(request, templates, session_factory, target)

    @app.get("/api/captures")
    def api_captures(
        day: str | None = None,
        _authed: str = Depends(auth),
    ) -> list[dict[str, Any]]:
        target = date.fromisoformat(day) if day else date.today()
        return _load_captures(session_factory, target)

    @app.get("/api/stats")
    def api_stats(
        day: str | None = None,
        _authed: str = Depends(auth),
    ) -> dict[str, Any]:
        target = date.fromisoformat(day) if day else date.today()
        captures = _load_captures(session_factory, target)
        blocks = _build_blocks(captures, capture_interval_seconds())
        return {
            "date": target.isoformat(),
            **_compute_stats_from_blocks(captures, blocks),
        }

    return app


def _render_timeline(
    request: Request,
    templates: Jinja2Templates,
    sessions: sessionmaker,
    target: date,
) -> HTMLResponse:
    captures = _load_captures(sessions, target)
    blocks = _build_blocks(captures, capture_interval_seconds())
    stats = _compute_stats_from_blocks(captures, blocks)

    # Sort categories by total time descending for the stats strip.
    stats_sorted = sorted(
        stats["seconds_per_category"].items(),
        key=lambda kv: -kv[1],
    )

    # Annotate blocks that conflict with a scheduled lesson (distraction
    # category during a school block), and collect them for a summary.
    conflicts = _annotate_conflicts(blocks, captures, capture_interval_seconds())
    school_break = _school_break_label(target)

    templates.env.filters.setdefault("fmt_duration", _fmt_duration)
    templates.env.filters.setdefault("fmt_clock", _fmt_clock)

    from familymon.heartbeat import capture_problems, check_capture_freshness, latest_heartbeat

    # The root watcher's verdict knows whether anyone was logged in; without
    # one (dev runs, no timer) fall back to plain capture freshness.
    heartbeat = latest_heartbeat(sessions) or check_capture_freshness(sessions)
    problems = capture_problems(sessions, *_day_bounds(target))

    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "target_date": target.isoformat(),
            "prev_day": (target - timedelta(days=1)).isoformat(),
            "next_day": (target + timedelta(days=1)).isoformat(),
            "blocks": blocks,
            "stats_sorted": stats_sorted,
            "total_captures": stats["total_captures"],
            "conflicts": conflicts,
            "school_break": school_break,
            "heartbeat": heartbeat,
            "problems": problems,
        },
    )


def _school_break_label(target: date) -> str | None:
    """'Half term 2026-10-19 to 2026-11-01' when the day falls in a schedule
    break (lessons out of effect, so no conflicts by design), else None.
    No-op without an imported schedule."""
    from familymon.config import schedule_path
    from familymon.policy.schedule import Schedule

    path = schedule_path()
    if not path.exists():
        return None
    br = Schedule.load(path).break_on(target)
    return br.label if br is not None else None


def _annotate_conflicts(
    blocks: list[dict[str, Any]],
    captures: list[dict[str, Any]],
    default_interval: float,
) -> list[dict[str, Any]]:
    """Compute lesson-time conflicts and return them for the summary panel.

    Uses the SAME per-capture `find_conflicts` the CLI uses, so the dashboard
    and `familymonctl conflicts` never disagree — an earlier version tested only
    each block's start timestamp, which mis-handled blocks straddling a lesson
    boundary. Each timeline block that overlaps a conflict episode of its
    category is tagged with `conflict_subject` for the red row. No-op if no
    schedule has been imported."""
    from familymon.config import schedule_path
    from familymon.policy.conflicts import find_conflicts
    from familymon.policy.schedule import Schedule

    path = schedule_path()
    if not path.exists():
        return []
    schedule = Schedule.load(path)

    episodes = find_conflicts(
        schedule,
        [
            (datetime.fromisoformat(c["captured_at"]), c["category"], c["active_title"])
            for c in captures
        ],
    )
    if not episodes:
        return []

    for b in blocks:
        b_start = datetime.fromisoformat(b["start"])
        b_end = datetime.fromisoformat(b["end"])
        for ep in episodes:
            if ep.category == b["category"] and ep.start <= b_end and ep.end >= b_start:
                b["conflict_subject"] = ep.block_subject
                break

    return [
        {
            "start": ep.start.isoformat(),
            "end": ep.end.isoformat(),
            "category": ep.category,
            "conflict_subject": ep.block_subject,
            "duration_seconds": ep.seconds(default_interval),
            "detail": ep.detail,
        }
        for ep in episodes
    ]
