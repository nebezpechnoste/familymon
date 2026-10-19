from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, time

import pytest

from familymon.policy.crx import ExtensionRef
from familymon.policy.enforcement import PolicyError
from familymon.policy.schedule import Schedule, ScheduleBlock
from familymon.policy.webblock import (
    BASE_POLICY,
    CHROME_BYPASS_RULE,
    DEFAULT_BLOCK_CATEGORIES,
    NEVER_BLOCK,
    PRIVATE_WINDOWS_POLICY,
    ProcessInfo,
    WebBlockConfig,
    build_blocklist,
    chrome_policy_document,
    close_forbidden,
    compile_masks,
    describe,
    forbidden_rule,
    load_domain_table,
    parse_web_block,
    plan_web_block,
    read_document,
    reconcile,
    scan_processes,
)

TABLE = {
    "messaging": ["web.whatsapp.com", "whatsapp.com", "discord.com", "meet.google.com", "zoom.us"],
    "social": ["tiktok.com", "reddit.com"],
    "games": ["roblox.com", "gameflare.com", "poki.com"],
    "video": ["youtube.com", "netflix.com"],
    "school": ["kingsinterhigh.co.uk"],
}

# Monday 2026-10-12: Maths 09:20-10:00 (school), Maths self-study 10:15-11:15.
SCHED = Schedule(
    blocks=[
        ScheduleBlock("Monday", time(9, 20), time(10, 0), "Maths"),
        ScheduleBlock("Monday", time(10, 15), time(11, 15), "Maths", category="study", notify=True),
    ]
)


def _mon(h, m):
    return datetime(2026, 10, 12, h, m)


# ---- config -----------------------------------------------------------------


def test_absent_section_means_disabled():
    cfg = parse_web_block({"user": "kid"})
    assert cfg.enabled is False
    assert cfg.categories == DEFAULT_BLOCK_CATEGORIES


def test_defaults_spare_video():
    assert "video" not in DEFAULT_BLOCK_CATEGORIES
    assert set(DEFAULT_BLOCK_CATEGORIES) == {"messaging", "social", "games"}


def test_parse_full_section():
    cfg = parse_web_block(
        {
            "web_block": {
                "enabled": True,
                "categories": ["messaging"],
                "extra_domains": ["Example.COM"],
                "allow_domains": ["support.discord.com"],
            }
        }
    )
    assert cfg == WebBlockConfig(
        enabled=True,
        categories=("messaging",),
        extra_domains=("example.com",),
        allow_domains=("support.discord.com",),
    )


@pytest.mark.parametrize(
    "section",
    [
        "yes",
        {"enabled": "true"},
        {"enabled": True, "categories": ["gaming"]},
        {"enabled": True, "extra_domains": ["bad host"]},
        {"enabled": True, "extra_domains": [""]},
        {"enabled": True, "allow_domains": 42},
    ],
)
def test_parse_rejects_bad_values(section):
    with pytest.raises(PolicyError):
        parse_web_block({"web_block": section})


# ---- block set ----------------------------------------------------------------


def test_blocklist_is_union_of_categories_minus_exemptions():
    cfg = WebBlockConfig(enabled=True)
    blocked, allowed = build_blocklist(cfg, TABLE)
    # Set algebra rather than `in`: `blocked` is a tuple of hosts, and a bare
    # `"x.com" in blocked` reads to CodeQL like a URL substring check.
    assert {"whatsapp.com", "tiktok.com", "gameflare.com"} <= set(blocked)
    assert {"youtube.com", "kingsinterhigh.co.uk"}.isdisjoint(blocked)
    # School conferencing is never blocked, even though the table files it as messaging.
    assert {"meet.google.com", "zoom.us"}.isdisjoint(blocked)
    assert set(NEVER_BLOCK) <= set(allowed)
    assert list(blocked) == sorted(set(blocked))  # stable, deduplicated


def test_extra_and_allow_domains():
    cfg = WebBlockConfig(
        enabled=True, categories=("games",), extra_domains=("example.com", "poki.com"),
        allow_domains=("roblox.com",),
    )
    blocked, allowed = build_blocklist(cfg, TABLE)
    assert blocked == ("example.com", "gameflare.com", "poki.com")
    assert {"roblox.com"} <= set(allowed)


def test_unknown_category_in_config_yields_nothing_from_it():
    cfg = WebBlockConfig(enabled=True, categories=("messaging", "homework"))
    blocked, _ = build_blocklist(cfg, TABLE)
    assert {"whatsapp.com"} <= set(blocked) and len(blocked) == 3


def test_load_domain_table_keeps_order_and_normalises(tmp_path):
    p = tmp_path / "domains.yaml"
    p.write_text("games:\n  - Poki.com.\n  - gameflare.com\nvideo:\n")
    table = load_domain_table(p)
    assert table == {"games": ["poki.com", "gameflare.com"], "video": []}


# ---- chrome document ------------------------------------------------------------


def test_document_outside_lesson_is_base_only():
    doc = chrome_policy_document((), ("meet.google.com",))
    assert doc == BASE_POLICY
    assert "URLBlocklist" not in doc and "URLAllowlist" not in doc


def test_document_during_lesson_has_lists():
    doc = chrome_policy_document(("whatsapp.com",), ("meet.google.com",))
    assert doc["URLBlocklist"] == ["whatsapp.com"]
    assert doc["URLAllowlist"] == ["meet.google.com"]
    assert doc["DnsOverHttpsMode"] == "off" and doc["EncryptedClientHelloEnabled"] is False


# ---- plan ---------------------------------------------------------------------


def test_plan_blocks_during_school_block_only():
    cfg = WebBlockConfig(enabled=True)
    during = plan_web_block(cfg, SCHED, _mon(9, 30), TABLE)
    assert during.block is not None and during.block.subject == "Maths"
    assert {"whatsapp.com"} <= set(during.document["URLBlocklist"])

    after = plan_web_block(cfg, SCHED, _mon(10, 5), TABLE)
    assert after.block is None and "URLBlocklist" not in after.document

    # Self-study extras are notification items, not lessons: no block.
    study = plan_web_block(cfg, SCHED, _mon(10, 30), TABLE)
    assert study.block is None and "URLBlocklist" not in study.document


def test_plan_end_boundary_is_exclusive():
    cfg = WebBlockConfig(enabled=True)
    assert plan_web_block(cfg, SCHED, _mon(9, 59), TABLE).block is not None
    assert plan_web_block(cfg, SCHED, _mon(10, 0), TABLE).block is None


def test_plan_disabled_wants_no_file():
    plan = plan_web_block(WebBlockConfig(enabled=False), SCHED, _mon(9, 30), TABLE)
    assert plan.document is None and plan.block is None
    assert "disabled" in describe(plan)


def test_plan_without_schedule_is_base_only():
    plan = plan_web_block(WebBlockConfig(enabled=True), None, _mon(9, 30), TABLE)
    assert plan.block is None and plan.document == BASE_POLICY
    assert plan.blocked  # the set is computed, just not applied


# ---- reconcile -----------------------------------------------------------------


def test_reconcile_writes_then_converges(tmp_path):
    path = tmp_path / "policies" / "managed" / "familymon.json"
    doc = chrome_policy_document(("whatsapp.com",), ("zoom.us",))
    assert reconcile(path, doc) is True
    assert json.loads(path.read_text()) == doc
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    # Same content: untouched (Chrome would otherwise reload for nothing).
    before = path.stat().st_mtime_ns
    assert reconcile(path, doc) is False
    assert path.stat().st_mtime_ns == before
    # Different content: rewritten.
    assert reconcile(path, chrome_policy_document((), ())) is True
    assert "URLBlocklist" not in json.loads(path.read_text())


def test_reconcile_never_leaves_temp_files_in_the_watched_dir(tmp_path):
    """Chrome loads *every* file in the managed dir, so the temp file must
    live outside it and only the final rename lands there."""
    managed = tmp_path / "policies" / "managed"
    path = managed / "familymon.json"
    reconcile(path, chrome_policy_document(("a.com",), ()))
    assert os.listdir(managed) == ["familymon.json"]
    assert not list(tmp_path.glob("policies/.*.tmp"))


def test_reconcile_removes_file_when_disabled(tmp_path):
    path = tmp_path / "managed" / "familymon.json"
    reconcile(path, chrome_policy_document((), ()))
    assert path.exists()
    assert reconcile(path, None) is True
    assert not path.exists()
    assert reconcile(path, None) is False


def test_reconcile_replaces_garbage(tmp_path):
    path = tmp_path / "managed" / "familymon.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert read_document(path) is None
    assert reconcile(path, chrome_policy_document((), ())) is True
    assert read_document(path) == BASE_POLICY


# ---- other browsers / forbidden apps -------------------------------------------

_ON = WebBlockConfig(enabled=True)


def _proc(exe, cmdline="", flatpak_id=None):
    return ProcessInfo(pid=1234, uid=1001, exe=exe, cmdline=cmdline, flatpak_id=flatpak_id)


@pytest.mark.parametrize(
    "exe, expected",
    [
        ("/snap/firefox/8995/usr/lib/firefox/firefox", "firefox"),
        ("/home/kid/Downloads/firefox/firefox-bin", "firefox"),           # tarball
        ("/home/kid/tor-browser/Browser/firefox.real", "tor-browser"),   # first component wins
        ("/tmp/.mount_FirefoXYZ/usr/lib/firefox/firefox", "firefox"),    # AppImage
        ("/snap/chromium/3000/usr/lib/chromium-browser/chrome", "chromium"),
        ("/opt/brave.com/brave/brave", "brave"),
        ("/opt/microsoft/msedge/msedge", "msedge"),
        ("/usr/lib/x86_64-linux-gnu/opera/opera", "opera"),
        ("/opt/google/chrome/chrome", None),  # Chrome: governed by the policy
        ("/usr/bin/zenity", None),                                        # not "zen"
        ("/home/kid/Downloads/cooperation/game", None),                   # not "opera"
        ("/usr/bin/python3", None),
    ],
)
def test_known_browsers_match_by_path_component(exe, expected):
    assert forbidden_rule(_proc(exe), _ON, []) == expected


def test_flatpak_browser_matches_by_flatpak_id():
    p = _proc("/app/lib/firefox/firefox", flatpak_id="org.mozilla.firefox")
    assert forbidden_rule(p, _ON, []) == "org.mozilla.firefox"
    # A flatpak that isn't a browser (Roblox via Sober) is left alone.
    sober = _proc("/app/bin/sober", flatpak_id="org.vinegarhq.Sober")
    assert forbidden_rule(sober, _ON, []) is None


def test_parent_masks_match_exe_and_cmdline():
    cfg = parse_web_block(
        {"web_block": {"enabled": True, "forbid_apps": ["myapp [My App]", ".*--kiosk.*"]}}
    )
    assert cfg.forbid_apps == (".*myapp.*", ".*--kiosk.*")
    pats = compile_masks(cfg)
    assert forbidden_rule(_proc("/opt/MyApp/myapp"), cfg, pats) == ".*myapp.*"
    kiosk = _proc("/usr/bin/thing", "thing --kiosk")
    assert forbidden_rule(kiosk, cfg, pats) == ".*--kiosk.*"
    assert forbidden_rule(_proc("/usr/bin/thing", "thing"), cfg, pats) is None


def test_forbid_other_browsers_can_be_turned_off():
    cfg = parse_web_block({"web_block": {"enabled": True, "forbid_other_browsers": False}})
    assert forbidden_rule(_proc("/snap/firefox/1/usr/lib/firefox/firefox"), cfg, []) is None


@pytest.mark.parametrize(
    "section",
    [
        {"enabled": True, "forbid_other_browsers": "no"},
        {"enabled": True, "forbid_apps": "firefox"},
        {"enabled": True, "forbid_apps": ["a;b"]},
        {"enabled": True, "forbid_apps": ["(["]},
    ],
)
def test_forbid_config_rejects_bad_values(section):
    with pytest.raises(PolicyError):
        parse_web_block({"web_block": section})


def test_scan_and_close_a_fake_browser(tmp_path):
    """End to end on real processes: a copy of perl under a uniquely named
    directory is found by uid, terminated, and reported; an unrelated
    process of ours survives.

    SAFETY: the built-in browser list is OFF here and the mask names only
    the fake path — the sweep runs against the developer's own uid, and the
    default list would close a real Firefox/Brave on the dev machine.
    (perl, not sleep: coreutils is a multicall binary that dispatches on
    argv[0] and would exit at once.)
    """
    perl = shutil.which("perl")
    if not perl:
        pytest.skip("perl not available")
    fake_dir = tmp_path / "fm-fake-browser"
    fake_dir.mkdir()
    fake = fake_dir / "fake-bin"
    shutil.copy(perl, fake)
    fake.chmod(0o755)
    cfg = WebBlockConfig(
        enabled=True, forbid_other_browsers=False, forbid_apps=(f".*{re.escape(str(fake))}.*",)
    )
    victim = subprocess.Popen([str(fake), "-e", "sleep 300"])
    bystander = subprocess.Popen([perl, "-e", "sleep 300"])
    try:
        procs = scan_processes(os.getuid())
        mine = {p.pid: p for p in procs}
        assert victim.pid in mine and mine[victim.pid].exe == str(fake)
        assert bystander.pid in mine
        closed = close_forbidden(procs, cfg, compile_masks(cfg), grace_seconds=3.0)
        assert [c.pid for c in closed] == [victim.pid]
        assert victim.wait(timeout=5) != 0
        assert bystander.poll() is None
    finally:
        for p in (victim, bystander):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)


def test_close_forbidden_dry_run_reports_without_killing():
    procs = [_proc("/snap/firefox/1/usr/lib/firefox/firefox")]
    closed = close_forbidden(procs, _ON, [], kill=False)
    assert len(closed) == 1 and closed[0].name == "firefox" and not closed[0].forced


# ---- open-tab closer extension + private windows ----------------------------

EXT = ExtensionRef(
    id="a" * 32,
    update_url="file:///usr/share/familymon/chrome-extension/update.xml",
    version="1.2610.13.1905",
)
PRIV_CFG = WebBlockConfig(enabled=True, forbid_other_browsers=False)


def test_document_force_installs_and_configures_the_extension():
    lesson = SCHED.blocks[0]
    doc = chrome_policy_document(
        ("roblox.com", "web.whatsapp.com"), ("meet.google.com",), extension=EXT, block=lesson
    )
    assert doc["ExtensionInstallForcelist"] == ["a" * 32 + ";" + EXT.update_url]
    assert doc["3rdparty"] == {
        "extensions": {
            "a" * 32: {
                "active": True,
                "blocked": ["roblox.com", "web.whatsapp.com"],
                "allowed": ["meet.google.com"],
                "subject": "Maths",
                "until": "10:00",
            }
        }
    }
    assert doc["DeveloperToolsAvailability"] == 0  # no DevTools on our extension
    assert "IncognitoModeAvailability" not in doc


def test_document_keeps_extension_installed_but_idle_outside_lessons():
    doc = chrome_policy_document((), (), extension=EXT)
    assert doc["ExtensionInstallForcelist"] == [EXT.forcelist_entry]
    assert doc["3rdparty"]["extensions"]["a" * 32] == {
        "active": False, "blocked": [], "allowed": [], "subject": "", "until": ""
    }
    assert "URLBlocklist" not in doc


def test_document_without_extension_has_no_extension_keys():
    doc = chrome_policy_document(("roblox.com",), ())
    assert "ExtensionInstallForcelist" not in doc and "3rdparty" not in doc


def test_plan_threads_the_extension_through_both_states():
    inside = plan_web_block(PRIV_CFG, SCHED, _mon(9, 30), TABLE, extension=EXT)
    outside = plan_web_block(PRIV_CFG, SCHED, _mon(12, 0), TABLE, extension=EXT)
    assert inside.extension is EXT and outside.extension is EXT
    assert inside.document["3rdparty"]["extensions"]["a" * 32]["active"] is True
    assert inside.document["3rdparty"]["extensions"]["a" * 32]["blocked"] == list(inside.blocked)
    assert outside.document["3rdparty"]["extensions"]["a" * 32]["active"] is False
    assert (
        outside.document["ExtensionInstallForcelist"]
        == inside.document["ExtensionInstallForcelist"]
    )


@pytest.mark.parametrize(
    "mode, inside_private, outside_private",
    [("lessons", True, False), ("always", True, True), ("off", False, False)],
)
def test_private_windows_follow_block_private(mode, inside_private, outside_private):
    cfg = WebBlockConfig(enabled=True, block_private=mode)
    inside = plan_web_block(cfg, SCHED, _mon(9, 30), TABLE).document
    outside = plan_web_block(cfg, SCHED, _mon(12, 0), TABLE).document
    for doc, expected in ((inside, inside_private), (outside, outside_private)):
        has = all(doc.get(k) == v for k, v in PRIVATE_WINDOWS_POLICY.items())
        assert has is expected
    assert PRIVATE_WINDOWS_POLICY == {
        "IncognitoModeAvailability": 1,
        "BrowserGuestModeEnabled": False,
    }


def test_block_private_parses_and_rejects():
    assert parse_web_block({"web_block": {"enabled": True}}).block_private == "lessons"
    cfg = parse_web_block({"web_block": {"enabled": True, "block_private": "always"}})
    assert cfg.block_private == "always"
    with pytest.raises(PolicyError, match="block_private"):
        parse_web_block({"web_block": {"enabled": True, "block_private": "sometimes"}})


# ---- Chrome launched without extensions ------------------------------------


def _chrome(cmdline: str, exe: str = "/opt/google/chrome/chrome") -> ProcessInfo:
    return ProcessInfo(pid=4321, uid=1001, exe=exe, cmdline=cmdline, flatpak_id=None)


@pytest.mark.parametrize(
    "cmdline",
    [
        "/opt/google/chrome/chrome --disable-extensions",
        "/opt/google/chrome/chrome --disable-extensions-except=/tmp/x --user-data-dir=/tmp/p",
        "/opt/google/chrome/chrome --type=renderer --disable-extensions --lang=en",
    ],
)
def test_chrome_without_extensions_is_closed_even_when_nothing_else_is(cmdline):
    off = WebBlockConfig(enabled=True, forbid_other_browsers=False)
    assert forbidden_rule(_chrome(cmdline), off, []) == CHROME_BYPASS_RULE
    assert forbidden_rule(_chrome(cmdline), WebBlockConfig(enabled=True), []) == CHROME_BYPASS_RULE


def test_plain_chrome_and_lookalikes_are_left_alone():
    on = WebBlockConfig(enabled=True)
    plain = _chrome("/opt/google/chrome/chrome --ozone-platform=wayland")
    assert forbidden_rule(plain, on, []) is None
    # Any switch starting with --disable-extensions counts.
    variant = _chrome("/opt/google/chrome/chrome --disable-extensions-file-access-check")
    assert forbidden_rule(variant, on, []) == CHROME_BYPASS_RULE
    # Only Chrome's own binary: another program mentioning the switch is not Chrome.
    other = _chrome("python3 run.py --disable-extensions", exe="/usr/bin/python3")
    assert forbidden_rule(other, on, []) is None


def test_plan_lifts_block_during_a_school_break():
    """A break in the schedule takes the lesson out of effect: base policy only."""
    from datetime import date, datetime, time

    from familymon.policy.schedule import Schedule, ScheduleBlock, SchoolBreak

    sched = Schedule(
        blocks=[ScheduleBlock("Monday", time(9, 20), time(10, 0), "Assembly")],
        breaks=[SchoolBreak(date(2026, 10, 19), date(2026, 11, 1), name="Half term")],
    )
    cfg = WebBlockConfig(enabled=True)
    in_break = plan_web_block(cfg, sched, datetime(2026, 10, 19, 9, 30), TABLE)
    assert in_break.block is None
    assert "URLBlocklist" not in in_break.document
    after = plan_web_block(cfg, sched, datetime(2026, 11, 2, 9, 30), TABLE)
    assert after.block is not None and after.block.subject == "Assembly"
    assert "URLBlocklist" in after.document
