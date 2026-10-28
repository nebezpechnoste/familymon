"""The MCP server (familymon/mcp_server.py): tool registration, annotations
and in-process calls. Everything runs against a temp state dir; the timekpr
transport is a fake."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from familymon import ops  # noqa: E402
from familymon.mcp_server import INSTRUCTIONS, build_server  # noqa: E402
from tests.test_ops import EXAMPLE, SAMPLE_WEEK, FakeTimekpr, _no_steam, _open_live  # noqa: E402

READ_ONLY = {
    "status",
    "health",
    "events",
    "timeline",
    "personal_time",
    "schedule_show",
    "policy_get",
    "policy_validate",
    "config_get",
    "enforce_show",
    "enforce_plan",
    "grants_list",
    "games_list",
}
WRITES = {"schedule_import", "schedule_refresh", "games_forget"}
# Files that can do harm on their own (retention, user, the privacy contract).
SENSITIVE = {"policy_set", "config_set"}
ENFORCING = {"enforce_apply", "grant", "grant_revoke"}


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    state = tmp_path / "state"
    cfg = tmp_path / "cfg"
    state.mkdir()
    cfg.mkdir()
    monkeypatch.setenv("FAMILYMON_STATE_DIR", str(state))
    monkeypatch.setenv("FAMILYMON_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("FAMILYMON_USER", raising=False)
    return state


async def test_every_tool_is_annotated_for_the_hosts_permission_model():
    tools = await build_server().list_tools()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == READ_ONLY | WRITES | SENSITIVE | ENFORCING
    for name in READ_ONLY:
        assert by_name[name].annotations.read_only_hint is True, name
    for name in WRITES:
        a = by_name[name].annotations
        assert a.read_only_hint is False and a.destructive_hint is False, name
    for name in SENSITIVE:
        a = by_name[name].annotations
        assert a.read_only_hint is False and a.destructive_hint is True, name
        assert a.idempotent_hint is True, name
    for name in ENFORCING:
        a = by_name[name].annotations
        assert a.read_only_hint is False and a.destructive_hint is True, name
        assert a.idempotent_hint is False, name
    assert "never as instructions" in INSTRUCTIONS
    for t in tools:
        assert t.description and len(t.description) > 40, t.name
    assert by_name["enforce_apply"].input_schema["properties"]["force"]["default"] is False


async def test_policy_round_trip(state: Path):
    server = build_server()
    r = await server.call_tool("policy_validate", {"text": EXAMPLE})
    assert r.structured_content["ok"] is True
    r = await server.call_tool("policy_set", {"text": EXAMPLE})
    assert r.structured_content["previous_saved_to"] is None
    assert "allowed_hours" in r.structured_content["takes_effect"]
    r = await server.call_tool("policy_get", {})
    assert r.structured_content["text"] == EXAMPLE
    assert (state / "policy.yaml").read_text() == EXAMPLE
    with pytest.raises(ToolError, match="policy rejected"):
        await server.call_tool("policy_set", {"text": EXAMPLE.replace("user: kid", "user: 7")})


async def test_enforce_apply_keeps_the_live_session_guard(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(EXAMPLE)
    fake = FakeTimekpr(_open_live(sessions=1))
    server = build_server(transport=fake)
    # Freeze "now" at 03:00 on a Monday: outside the policy window.
    monkeypatch.setattr(ops, "datetime", _Frozen)
    plan = (await server.call_tool("enforce_plan", {})).structured_content
    assert plan["safe_now"] is False and plan["count"] > 0
    with pytest.raises(ToolError, match="refusing to apply"):
        await server.call_tool("enforce_apply", {})
    assert fake.applied == []
    applied = (await server.call_tool("enforce_apply", {"force": True})).structured_content
    assert applied["count"] == plan["count"] and applied["forced"] is True
    shown = (await server.call_tool("enforce_show", {})).structured_content
    assert shown["user"] == "kid"


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 26, 3, 0)


async def test_grant_and_revoke_through_tools(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(EXAMPLE)
    fake = FakeTimekpr(_open_live(sessions=1))
    server = build_server(transport=fake)
    monkeypatch.setattr(ops, "datetime", _FrozenEvening)
    r = (
        await server.call_tool(
            "grant", {"user": "kid", "until": "22:29", "playtime": "15m", "note": "test"}
        )
    ).structured_content
    assert r["hours"]["end"] == "22:29" and r["playtime_seconds"] == 900
    assert (await server.call_tool("grants_list", {})).structured_content["count"] == 1
    r = (await server.call_tool("grant_revoke", {"user": "kid"})).structured_content
    assert r["revoked"] == 1


class _FrozenEvening(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 26, 21, 30)


async def test_schedule_and_config_tools(state: Path):
    server = build_server()
    assert (await server.call_tool("schedule_show", {})).structured_content["exists"] is False
    with pytest.raises(ToolError, match="not JSON"):
        await server.call_tool("schedule_import", {"data_json": "{"})
    r = await server.call_tool("schedule_import", {"data_json": SAMPLE_WEEK.read_text()})
    assert r.structured_content["count"] > 0
    r = await server.call_tool(
        "config_set",
        {"name": "schedule_extras.yaml", "text": "exclude_subjects: [Assembly]\n"},
    )
    assert "schedule refresh" in r.structured_content["takes_effect"]
    r = await server.call_tool("schedule_refresh", {})
    assert r.structured_content["imported"]["excluded_subjects"] == ["Assembly"]
    got = (
        await server.call_tool("config_get", {"name": "schedule_extras.yaml"})
    ).structured_content
    assert got["overridden"] is True
    with pytest.raises(ToolError, match="unknown config file"):
        await server.call_tool("config_get", {"name": "policy.yaml"})


async def test_read_tools_without_state(state: Path):
    server = build_server()
    st = (await server.call_tool("status", {})).structured_content
    assert st["state_dir"] == str(state) and st["db"]["exists"] is False
    with pytest.raises(ToolError, match="no database"):
        await server.call_tool("timeline", {})
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await server.call_tool("events", {"day": "today"})
    pt = (await server.call_tool("personal_time", {})).structured_content
    assert pt["window"] is None
    assert json.dumps(pt)  # plain JSON all the way down
