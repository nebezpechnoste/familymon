"""Heuristic-only classifier.

Pre-LLM signal layer: cheap, fast, fully local. The cost story depends on this
resolving the majority of captures so the VLM only sees the ambiguous ones.

Per capture we look at ALL visible windows (not just the focused one) and pick
the highest-priority category among them. That closes the messaging-skip
loophole at the classification level: a focused chat window can't mask a
visible game or video, because games/video outrank a messaging overlay in the
priority order. This is the "priority" model — exactly one category per
capture, no schema change.

  PLANNED UPGRADE — hybrid: keep this priority-chosen *primary* category but
  also record the full *set* of categories present per capture (multi-label),
  for lossless multi-activity records and overlap-aware stats ("a game was on
  screen for N of M school-hour minutes" independent of focus). That needs a
  schema addition (a capture_id->category side table or JSON array). Tracked in
  task #18 / see README.

Per-window signal sources:
  1. App lookup (.desktop scan) — native app identity, highest confidence.
  2. Browser windows: domain-in-title (most specific) -> brand-in-title
     (stopgap until real URL extraction) -> generic BROWSING_OTHER.

Captures suppressed for session reasons (locked / backgrounded) classify as
IDLE rather than polluting UNKNOWN.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from familymon.analyze.categories import Category
from familymon.analyze.schema import Classification
from familymon.categorize.apps import AppCategoryLookup
from familymon.categorize.brands import BrandTitleLookup
from familymon.categorize.domains import DomainCategoryLookup

# Browser WM_CLASSes (snap or otherwise). For these we try domain/brand title
# extraction before falling back to generic browsing.
_BROWSER_CLASSES = frozenset(
    {
        "firefox",
        "firefox-esr",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "chrome",
        "brave",
        "brave-browser",
        "vivaldi",
        "vivaldi-stable",
        "opera",
        "librewolf",
        "zen",
    }
)

# Most monitoring-salient first. When several category-bearing windows are
# visible, the lowest index wins. Leisure/distraction (games, video, social)
# surfaces above productive work and above a messaging overlay.
_PRIORITY: tuple[Category, ...] = (
    Category.GAMES,
    Category.VIDEO,
    Category.SOCIAL,
    Category.SCHOOL,
    Category.HOMEWORK,
    Category.CODE,
    Category.PRODUCTIVITY,
    Category.MESSAGING,
    Category.BROWSING_OTHER,
    Category.IDLE,
    Category.UNKNOWN,
)
_PRIORITY_INDEX = {c: i for i, c in enumerate(_PRIORITY)}

_SESSION_IDLE_REASONS = frozenset({"session_locked", "session_inactive"})

_DOMAIN_IN_TITLE_RE = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WindowView:
    """Minimal per-window data the classifier needs. Decoupled from SQLAlchemy
    rows so tests don't need a DB."""

    wm_class: str | None = None
    title: str | None = None
    active: bool = False
    minimized: bool = False
    on_current_desktop: bool = True

    @property
    def hidden(self) -> bool:
        """Not on screen: minimized, or on another virtual desktop/activity."""
        return self.minimized or not self.on_current_desktop


@dataclass(frozen=True)
class _WindowVerdict:
    category: Category
    confidence: float
    evidence: str
    source: str  # "heuristic" | "domain"


def looks_like_browser(wm_class: str | None) -> bool:
    """Is this WM_CLASS a web browser? (Also used by the PlayTime charge-back
    reconciler to find game time that timekpr can't see inside a browser.)"""
    if not wm_class:
        return False
    normalized = wm_class.strip().lower()
    return any(c in _BROWSER_CLASSES for c in (normalized, *normalized.split("_")))


_looks_like_browser = looks_like_browser


def _domain_candidates_from_title(title: str | None) -> list[str]:
    if not title:
        return []
    return [m.group(1).lower() for m in _DOMAIN_IN_TITLE_RE.finditer(title)]


class HeuristicClassifier:
    def __init__(
        self,
        apps: AppCategoryLookup,
        domains: DomainCategoryLookup,
        brands: BrandTitleLookup,
    ) -> None:
        self._apps = apps
        self._domains = domains
        self._brands = brands

    def _classify_window(self, w: WindowView) -> _WindowVerdict | None:
        if w.hidden:
            return None

        if _looks_like_browser(w.wm_class):
            for candidate in _domain_candidates_from_title(w.title):
                cat = self._domains.lookup(candidate)
                if cat is not None:
                    return _WindowVerdict(cat, 0.7, f"domain={candidate}", "domain")
            brand = self._brands.lookup(w.title)
            if brand is not None:
                cat, keyword = brand
                return _WindowVerdict(cat, 0.6, f"brand={keyword!r}", "heuristic")
            return _WindowVerdict(
                Category.BROWSING_OTHER, 0.5, f"browser={w.wm_class!r}", "heuristic"
            )

        cat = self._apps.lookup(w.wm_class)
        if cat is not None:
            return _WindowVerdict(cat, 0.85, f"app={w.wm_class!r}", "heuristic")
        return None

    def classify(
        self,
        capture_id: int,
        windows: list[WindowView],
        *,
        suppressed_reason: str | None = None,
        game_running: bool = False,
    ) -> Classification:
        # A locked / backgrounded session is idle time, not "unknown".
        if suppressed_reason in _SESSION_IDLE_REASONS:
            return Classification(
                capture_id=capture_id,
                category=Category.IDLE,
                confidence=0.9,
                evidence_text=f"session {suppressed_reason}",
                source="heuristic",
                present_categories=[Category.IDLE],
            )

        verdicts = [v for v in (self._classify_window(w) for w in windows) if v is not None]
        # Trusted game detection (an installed game's process owns a window) is
        # authoritative — inject a GAMES verdict so it wins on priority, even if
        # the game's window isn't in our .desktop scan.
        if game_running:
            verdicts.append(
                _WindowVerdict(Category.GAMES, 0.95, "installed game process", "game_process")
            )
        if not verdicts:
            return Classification(
                capture_id=capture_id,
                category=Category.UNKNOWN,
                confidence=0.1,
                evidence_text="no signal from any visible window",
                source="heuristic",
                present_categories=[Category.UNKNOWN],
            )

        # Highest priority (lowest index); ties broken by higher confidence.
        best = min(verdicts, key=lambda v: (_PRIORITY_INDEX[v.category], -v.confidence))
        # All distinct categories present, priority-ordered (present[0] == best).
        present = sorted({v.category for v in verdicts}, key=lambda c: _PRIORITY_INDEX[c])
        n_visible = sum(1 for w in windows if not w.hidden)
        plural = "s" if n_visible != 1 else ""
        return Classification(
            capture_id=capture_id,
            category=best.category,
            confidence=best.confidence,
            evidence_text=f"{best.evidence} (of {n_visible} visible window{plural})",
            source=best.source,  # type: ignore[arg-type]
            present_categories=present,
        )
