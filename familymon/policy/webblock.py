"""Lesson-time web blocking through Chrome's managed policy.

While a school block is in progress, familymon writes a `URLBlocklist` into
Chrome's machine-wide policy file; outside lessons the key is dropped. Chrome
enforces it inside the browser (its "blocked by your administrator" page),
the file is root-owned so the monitored user can't edit it, and Chrome
watches the directory and reloads policy within seconds — no restart.

Scope mirrors the conflict detector's always-distraction set: messaging,
social, games. `video` is deliberately spared — a lesson may use YouTube —
and streaming video during a lesson is still *flagged*, just not blocked.
Conferencing endpoints a school may use (Meet, Zoom, Teams) are never
blocked, even though the domain table files them under messaging.

Known gaps, by design for now: a tab already open when the block starts
keeps its live connection until it navigates (Chrome applies the list to
navigations); native apps (Telegram Desktop, Steam, Sober) are timekpr's
domain; the file only governs Chrome (the only browser on the target — the
Firefox snap was removed).

The reconciler is stateless: desired content is a pure function of
(policy.yaml, schedule, now), compared against what's on disk, written only
on change. A missed tick or reboot self-corrects on the next minute.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from familymon.analyze.categories import Category
from familymon.policy.crx import ExtensionRef
from familymon.policy.enforcement import PolicyError
from familymon.policy.schedule import Schedule, ScheduleBlock

# Chrome reads every file in this directory (any name), lexically merged.
CHROME_POLICY_DIR = Path("/etc/opt/chrome/policies/managed")
POLICY_FILE_NAME = "familymon.json"

DEFAULT_BLOCK_CATEGORIES: tuple[str, ...] = ("messaging", "social", "games")

# School conferencing — never blocked, whatever the domain table says.
NEVER_BLOCK: tuple[str, ...] = ("meet.google.com", "teams.microsoft.com", "zoom.us")

# Always-on base policy: the browser must use the system resolver so a
# future DNS layer (and its query log) sees everything. No behaviour change
# today — Chrome's "automatic" mode never upgrades a LAN router to DoH.
BASE_POLICY: dict = {
    "DnsOverHttpsMode": "off",
    "EncryptedClientHelloEnabled": False,
    # 0 = no DevTools on policy-installed extensions (ours), allowed elsewhere.
    # Pinned so the lesson-block extension cannot be poked at from inside.
    "DeveloperToolsAvailability": 0,
}

# Added while private windows are blocked: Incognito off, Guest profile off.
PRIVATE_WINDOWS_POLICY: dict = {
    "IncognitoModeAvailability": 1,
    "BrowserGuestModeEnabled": False,
}


@dataclass(frozen=True)
class WebBlockConfig:
    enabled: bool = False
    categories: tuple[str, ...] = DEFAULT_BLOCK_CATEGORIES
    extra_domains: tuple[str, ...] = ()
    allow_domains: tuple[str, ...] = ()
    # Close other browsers during a lesson — they would bypass the Chrome policy.
    forbid_other_browsers: bool = True
    # Extra process masks to close during a lesson (games.extra_masks syntax:
    # regex full-matched against "exe cmdline", bare fragments wrapped in .*).
    forbid_apps: tuple[str, ...] = ()
    # Private windows (Incognito, Guest) run the URL block but not our
    # tab-closing extension, and an incognito tab opened before the lesson is
    # otherwise invisible to it: "lessons" disables them while a lesson is in
    # progress, "always" at all times, "off" never.
    block_private: str = "lessons"


PRIVATE_MODES: tuple[str, ...] = ("lessons", "always", "off")


_MASK_RE = re.compile(r"^([^\[\]]+?)\s*(?:\[([^\[\]]*)\])?$")


def _parse_masks(node: object, what: str) -> tuple[str, ...]:
    if node is None:
        return ()
    if not isinstance(node, list):
        raise PolicyError(f"{what} must be a list: {node!r}")
    out = []
    for item in node:
        m = _MASK_RE.match(str(item).strip())
        if not m or ";" in m.group(1):
            raise PolicyError(f"bad mask in {what} (no ';' or nested '[]'): {item!r}")
        mask = m.group(1).strip()
        if not mask.startswith(".*"):
            mask = f".*{mask}.*"
        try:
            re.compile(mask)
        except re.error as e:
            raise PolicyError(f"bad regex in {what}: {item!r} ({e})") from None
        out.append(mask)
    return tuple(out)


def _str_list(node: object, what: str) -> tuple[str, ...]:
    if node is None:
        return ()
    if isinstance(node, str):
        node = [node]
    if not isinstance(node, list):
        raise PolicyError(f"{what} must be a list of strings: {node!r}")
    out = []
    for item in node:
        if not isinstance(item, str) or not item.strip():
            raise PolicyError(f"{what} entries must be non-empty strings: {item!r}")
        text = item.strip().lower()
        if any(ch.isspace() for ch in text):
            raise PolicyError(f"{what} entry contains whitespace: {item!r}")
        out.append(text)
    return tuple(out)


def parse_web_block(policy: dict) -> WebBlockConfig:
    """policy.yaml dict -> WebBlockConfig (disabled when the section is absent)."""
    node = policy.get("web_block")
    if node is None:
        return WebBlockConfig()
    if not isinstance(node, dict):
        raise PolicyError(f"web_block must be a mapping: {node!r}")
    enabled = node.get("enabled", False)
    if not isinstance(enabled, bool):
        raise PolicyError(f"web_block.enabled must be true/false: {enabled!r}")
    categories = _str_list(
        node.get("categories", list(DEFAULT_BLOCK_CATEGORIES)), "web_block.categories"
    )
    known = {c.value for c in Category}
    unknown = [c for c in categories if c not in known]
    if unknown:
        raise PolicyError(f"web_block.categories has unknown categor(y/ies): {unknown}")
    forbid = node.get("forbid_other_browsers", True)
    if not isinstance(forbid, bool):
        raise PolicyError(f"web_block.forbid_other_browsers must be true/false: {forbid!r}")
    private = node.get("block_private", "lessons")
    if private not in PRIVATE_MODES:
        raise PolicyError(
            f"web_block.block_private must be one of {list(PRIVATE_MODES)}: {private!r}"
        )
    return WebBlockConfig(
        enabled=enabled,
        categories=categories,
        extra_domains=_str_list(node.get("extra_domains"), "web_block.extra_domains"),
        allow_domains=_str_list(node.get("allow_domains"), "web_block.allow_domains"),
        forbid_other_browsers=forbid,
        forbid_apps=_parse_masks(node.get("forbid_apps"), "web_block.forbid_apps"),
        block_private=private,
    )


def load_domain_table(path: Path) -> dict[str, list[str]]:
    """Raw category -> [domain, ...] mapping from a domains YAML, in file
    order. (The classifier's lookup is first-match and loses duplicates; for
    blocking we want every listed domain of the chosen categories.)"""
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise PolicyError(f"domain table is not a mapping: {path}")
    table: dict[str, list[str]] = {}
    for cat, domains in raw.items():
        table[str(cat)] = [
            str(d).strip().lower().rstrip(".") for d in (domains or []) if str(d).strip()
        ]
    return table


def build_blocklist(
    cfg: WebBlockConfig, table: dict[str, list[str]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(blocked, allowed) Chrome URL patterns. A bare host pattern covers the
    host and all its subdomains, which is exactly how the domain table is
    written. Sorted + deduplicated so the on-disk document is stable."""
    allowed = sorted(set(NEVER_BLOCK) | set(cfg.allow_domains))
    exempt = set(allowed)
    blocked: set[str] = set()
    for cat in cfg.categories:
        blocked.update(table.get(cat, []))
    blocked.update(cfg.extra_domains)
    blocked -= exempt
    return tuple(sorted(blocked)), tuple(allowed)


def chrome_policy_document(
    blocked: Sequence[str],
    allowed: Sequence[str],
    *,
    extension: ExtensionRef | None = None,
    block: ScheduleBlock | None = None,
    private_windows: bool = False,
) -> dict:
    """The JSON Chrome loads. Without `blocked` only the base policy remains —
    dropping the key is how a policy is unset.

    With a packaged extension the document also force-installs it and hands
    it the same block set through `3rdparty` (its managed storage), so tabs
    that were already open when the lesson started get closed too.
    URLBlocklist alone only stops *new* navigations.
    """
    doc = dict(BASE_POLICY)
    if private_windows:
        doc.update(PRIVATE_WINDOWS_POLICY)
    if blocked:
        doc["URLBlocklist"] = list(blocked)
        if allowed:
            doc["URLAllowlist"] = list(allowed)
    if extension is not None:
        doc["ExtensionInstallForcelist"] = [extension.forcelist_entry]
        doc["3rdparty"] = {
            "extensions": {
                extension.id: {
                    "active": bool(blocked),
                    "blocked": list(blocked),
                    "allowed": list(allowed) if blocked else [],
                    "subject": block.subject if block is not None else "",
                    "until": f"{block.end:%H:%M}" if block is not None else "",
                }
            }
        }
    return doc


@dataclass(frozen=True)
class WebBlockPlan:
    # The lesson in progress (None outside school blocks).
    block: ScheduleBlock | None
    # Desired file content; None means "our file must not exist" (disabled).
    document: dict | None
    blocked: tuple[str, ...] = ()
    # The packaged tab-closing extension, when the install shipped one.
    extension: ExtensionRef | None = None


def plan_web_block(
    cfg: WebBlockConfig,
    schedule: Schedule | None,
    now: datetime,
    table: dict[str, list[str]],
    *,
    extension: ExtensionRef | None = None,
) -> WebBlockPlan:
    if not cfg.enabled:
        return WebBlockPlan(block=None, document=None)
    block = schedule.block_at(now, category="school") if schedule is not None else None
    blocked, allowed = build_blocklist(cfg, table)
    private = cfg.block_private == "always" or (
        cfg.block_private == "lessons" and block is not None
    )
    if block is None:
        return WebBlockPlan(
            block=None,
            document=chrome_policy_document(
                (), (), extension=extension, private_windows=private
            ),
            blocked=blocked,
            extension=extension,
        )
    return WebBlockPlan(
        block=block,
        document=chrome_policy_document(
            blocked, allowed, extension=extension, block=block, private_windows=private
        ),
        blocked=blocked,
        extension=extension,
    )


def describe_extension(extension: ExtensionRef | None) -> str:
    if extension is None:
        return "open-tab closer: not packaged (this build ships no Chrome extension)"
    return (
        f"open-tab closer: extension {extension.id} v{extension.version} "
        f"via {extension.update_url}"
    )


def read_document(path: Path) -> dict | None:
    """Current on-disk document, or None when missing/unreadable/not JSON."""
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def reconcile(path: Path, document: dict | None) -> bool:
    """Make `path` hold `document` (or not exist). Returns True if anything
    changed on disk.

    The temp file is written *outside* the managed dir (its parent) and
    renamed in: Chrome loads every file in that directory, so a half-written
    sibling could be picked up as policy.
    """
    current = read_document(path)
    if document is None:
        if path.exists():
            path.unlink()
            return True
        return False
    if current == document:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def describe(plan: WebBlockPlan) -> str:
    if plan.document is None:
        return "web_block disabled — Chrome policy file removed if present"
    if plan.block is None:
        return f"no lesson in progress — {len(plan.blocked)} domain(s) unblocked"
    b = plan.block
    return (
        f"lesson in progress: {b.subject} {b.start:%H:%M}\u2013{b.end:%H:%M} \u2014 "
        f"{len(plan.blocked)} domain(s) blocked in Chrome"
    )


def domains_preview(domains: Iterable[str], limit: int = 12) -> str:
    items = list(domains)
    head = ", ".join(items[:limit])
    return head + (f", … (+{len(items) - limit})" if len(items) > limit else "")


# ---------------------------------------------------------------------------
# Other browsers / forbidden apps during a lesson.
#
# The Chrome policy only governs Chrome; any other browser would walk around
# it. timekpr can't express "not during lessons" (PlayTime masks carry day
# budgets, no time-of-day windows, and share the game budget), so the same
# per-minute tick closes them itself: every process of the monitored user
# whose exe path has a path component naming a known browser (deb, snap,
# AppImage and tarball forms all keep the name in the path), or whose
# FLATPAK_ID is a known browser, or that matches a parent-supplied mask.
# Chrome is never on the list. Matching is by path *component* (exact or
# followed by - . _), not substring: "zenity" is not "zen", "cooperation"
# is not "opera".
# ---------------------------------------------------------------------------

DEFAULT_FORBIDDEN_BROWSERS: tuple[str, ...] = (
    "firefox", "librewolf", "waterfox", "floorp", "palemoon", "seamonkey", "basilisk",
    "mullvad-browser", "tor-browser", "zen-browser", "zen_browser",
    "chromium", "brave", "vivaldi", "opera", "msedge", "microsoft-edge", "thorium",
    "epiphany", "falkon", "konqueror", "qutebrowser", "midori", "otter-browser",
)

DEFAULT_FORBIDDEN_FLATPAKS: tuple[str, ...] = (
    "org.mozilla.firefox", "io.gitlab.librewolf-community", "one.ablaze.floorp",
    "app.zen_browser.zen", "net.mullvad.mullvadbrowser", "org.torproject.torbrowser-launcher",
    "org.chromium.chromium", "com.github.eloston.ungoogledchromium",
    "io.github.ungoogled_software.ungoogled_chromium", "com.brave.browser",
    "com.vivaldi.vivaldi", "com.opera.opera", "com.microsoft.edge", "org.gnome.epiphany",
    "org.kde.falkon", "org.kde.konqueror", "org.qutebrowser.qutebrowser",
)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    uid: int
    exe: str  # resolved executable path
    cmdline: str  # arguments, space-joined
    flatpak_id: str | None = None  # FLATPAK_ID from the environment, if sandboxed


def _component_hit(exe: str, names: Sequence[str]) -> str | None:
    for comp in (c.lower() for c in exe.split("/") if c):
        for name in names:
            if comp == name or comp.startswith((f"{name}-", f"{name}.", f"{name}_")):
                return name
    return None


def compile_masks(cfg: WebBlockConfig) -> list[re.Pattern]:
    return [re.compile(m, re.IGNORECASE | re.DOTALL) for m in cfg.forbid_apps]


# Chrome itself is never a forbidden browser, but launched with any of these
# switches it runs without the force-installed tab closer (verified on the
# target: no extension loads under --disable-extensions). Such a Chrome is
# closed during lessons like any other bypass.
CHROME_BYPASS_SWITCHES: tuple[str, ...] = ("--disable-extensions",)
CHROME_BYPASS_RULE = "chrome (extensions disabled)"


def _chrome_bypass(proc: ProcessInfo) -> str | None:
    if _component_hit(proc.exe, ("chrome", "google-chrome")) is None:
        return None
    for arg in proc.cmdline.split():
        if any(arg.startswith(sw) for sw in CHROME_BYPASS_SWITCHES):
            return CHROME_BYPASS_RULE
    return None


def forbidden_rule(
    proc: ProcessInfo, cfg: WebBlockConfig, patterns: Sequence[re.Pattern]
) -> str | None:
    """The rule that forbids this process during a lesson, or None."""
    bypass = _chrome_bypass(proc)
    if bypass:
        return bypass
    if cfg.forbid_other_browsers:
        if proc.flatpak_id and proc.flatpak_id.lower() in DEFAULT_FORBIDDEN_FLATPAKS:
            return proc.flatpak_id
        hit = _component_hit(proc.exe, DEFAULT_FORBIDDEN_BROWSERS)
        if hit:
            return hit
    hay = f"{proc.exe} {proc.cmdline}"
    for pat in patterns:
        if pat.fullmatch(hay):
            return pat.pattern
    return None


def scan_processes(uid: int, proc_root: Path = Path("/proc")) -> list[ProcessInfo]:
    """Live processes of `uid` (ours excluded). Unreadable entries are skipped."""
    out: list[ProcessInfo] = []
    me = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        puid = next(
            (int(line.split()[1]) for line in status.splitlines() if line.startswith("Uid:")),
            None,
        )
        if puid != uid:
            continue
        try:
            exe = os.readlink(entry / "exe").removesuffix(" (deleted)")
        except OSError:
            continue  # kernel thread, zombie, or not ours to see
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            raw = b""
        cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        flatpak_id = None
        try:
            for item in (entry / "environ").read_bytes().split(b"\0"):
                if item.startswith(b"FLATPAK_ID="):
                    flatpak_id = item[len(b"FLATPAK_ID="):].decode("utf-8", "replace")
                    break
        except OSError:
            pass
        out.append(ProcessInfo(int(entry.name), puid, exe, cmdline, flatpak_id))
    return out


def _alive(pid: int, proc_root: Path) -> bool:
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return False
    # "pid (comm) S ..." — comm may contain spaces/parens; state follows the last ')'.
    return stat.rsplit(")", 1)[-1].split()[0] != "Z"


@dataclass(frozen=True)
class Closed:
    pid: int
    exe: str
    rule: str
    forced: bool = False  # survived SIGTERM, got SIGKILL

    @property
    def name(self) -> str:
        return self.rule.split(".")[-1] if "." in self.rule and " " not in self.rule else self.rule


def close_forbidden(
    procs: Sequence[ProcessInfo],
    cfg: WebBlockConfig,
    patterns: Sequence[re.Pattern],
    *,
    kill: bool = True,
    grace_seconds: float = 3.0,
    proc_root: Path = Path("/proc"),
) -> list[Closed]:
    """Terminate every forbidden process (SIGTERM, then SIGKILL after the
    grace period). With kill=False just report what would be closed."""
    hits = [(p, rule) for p in procs if (rule := forbidden_rule(p, cfg, patterns))]
    if not hits or not kill:
        return [Closed(p.pid, p.exe, rule) for p, rule in hits]
    for p, _ in hits:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(p.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    survivors = {p.pid for p, _ in hits}
    while survivors and time.monotonic() < deadline:
        survivors = {pid for pid in survivors if _alive(pid, proc_root)}
        if survivors:
            time.sleep(0.2)
    for pid in survivors:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    return [Closed(p.pid, p.exe, rule, forced=p.pid in survivors) for p, rule in hits]
