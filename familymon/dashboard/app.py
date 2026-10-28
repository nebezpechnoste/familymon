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
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import sessionmaker

from familymon.config import capture_interval_seconds, db_path
from familymon.storage.session import make_readonly_engine, make_session_factory
from familymon.timeline import (
    annotate_conflicts,
    build_blocks,
    compute_stats_from_blocks,
    day_bounds,
    load_captures,
    personal_time_spans,
    school_break_label,
)

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


def _fmt_duration(seconds: float) -> str:
    s = round(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h, rem = divmod(s, 3600)
    return f"{h}h{rem // 60:02d}m"


def _fmt_clock(iso_ts: str) -> str:
    """Render the HH:MM:SS portion of an ISO-8601 timestamp."""
    return iso_ts[11:19]


def _parse_day(day: str | None) -> date:
    if not day:
        return date.today()
    try:
        return date.fromisoformat(day)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid date") from e


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
        target = _parse_day(day)
        return load_captures(session_factory, target)

    @app.get("/api/stats")
    def api_stats(
        day: str | None = None,
        _authed: str = Depends(auth),
    ) -> dict[str, Any]:
        target = _parse_day(day)
        captures = load_captures(session_factory, target)
        blocks = build_blocks(captures, capture_interval_seconds())
        return {
            "date": target.isoformat(),
            **compute_stats_from_blocks(captures, blocks),
        }

    return app


def _render_timeline(
    request: Request,
    templates: Jinja2Templates,
    sessions: sessionmaker,
    target: date,
) -> HTMLResponse:
    captures = load_captures(sessions, target)
    blocks = build_blocks(captures, capture_interval_seconds())
    stats = compute_stats_from_blocks(captures, blocks)

    # Sort categories by total time descending for the stats strip.
    stats_sorted = sorted(
        stats["seconds_per_category"].items(),
        key=lambda kv: -kv[1],
    )

    # Annotate blocks that conflict with a scheduled lesson (distraction
    # category during a school block), and collect them for a summary.
    conflicts = annotate_conflicts(blocks, captures, capture_interval_seconds())
    school_break = school_break_label(target)

    templates.env.filters.setdefault("fmt_duration", _fmt_duration)
    templates.env.filters.setdefault("fmt_clock", _fmt_clock)

    from familymon.heartbeat import capture_problems, check_capture_freshness, latest_heartbeat

    # The root watcher's verdict knows whether anyone was logged in; without
    # one (dev runs, no timer) fall back to plain capture freshness.
    heartbeat = latest_heartbeat(sessions) or check_capture_freshness(sessions)
    problems = capture_problems(sessions, *day_bounds(target))

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
            "personal_spans": personal_time_spans(blocks),
        },
    )
