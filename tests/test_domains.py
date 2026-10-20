from pathlib import Path

import pytest

from familymon.analyze.categories import Category
from familymon.categorize.domains import DomainCategoryLookup, _suffix_apexes


def test_suffix_apexes_for_subdomain():
    assert _suffix_apexes("m.youtube.com") == ["m.youtube.com", "youtube.com", "com"]


def test_suffix_apexes_for_apex():
    assert _suffix_apexes("youtube.com") == ["youtube.com", "com"]


def test_suffix_apexes_strips_trailing_dot():
    assert _suffix_apexes("youtube.com.") == ["youtube.com", "com"]


def test_lookup_exact_apex():
    lookup = DomainCategoryLookup()
    lookup.add("youtube.com", Category.VIDEO)
    assert lookup.lookup("youtube.com") is Category.VIDEO


def test_lookup_subdomain_matches_apex():
    lookup = DomainCategoryLookup()
    lookup.add("youtube.com", Category.VIDEO)
    assert lookup.lookup("m.youtube.com") is Category.VIDEO
    assert lookup.lookup("studio.youtube.com") is Category.VIDEO


def test_lookup_unrelated_domain_returns_none():
    lookup = DomainCategoryLookup()
    lookup.add("youtube.com", Category.VIDEO)
    assert lookup.lookup("example.com") is None
    assert lookup.lookup(None) is None
    assert lookup.lookup("") is None


@pytest.fixture
def starter_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "doms.yaml"
    path.write_text(
        "video:\n  - youtube.com\ngames:\n  - lichess.org\n",
        encoding="utf-8",
    )
    return path


def test_from_yaml(starter_yaml: Path):
    lookup = DomainCategoryLookup.from_yaml(starter_yaml)
    assert lookup.lookup("youtube.com") is Category.VIDEO
    assert lookup.lookup("lichess.org") is Category.GAMES
    assert len(lookup) == 2


def test_packaged_starter_yaml_loads():
    """The shipped starter list parses and contains known anchors."""
    lookup = DomainCategoryLookup.from_yaml()
    assert lookup.lookup("youtube.com") is Category.VIDEO
    assert lookup.lookup("github.com") is Category.CODE
    assert lookup.lookup("web.whatsapp.com") is Category.MESSAGING


def test_entertainment_domains_are_school_time_violations():
    """TikTok/Max must land in categories that conflict during a lesson.
    TikTok is social (always a conflict); Max/HBO Max are video (flagged by the
    streaming domain-form check in policy/conflicts.py)."""
    lookup = DomainCategoryLookup.from_yaml()
    assert lookup.lookup("tiktok.com") is Category.SOCIAL
    assert lookup.lookup("max.com") is Category.VIDEO
    assert lookup.lookup("hbomax.com") is Category.VIDEO
