from datetime import datetime, timezone

from familymon.analyze.categories import Category
from familymon.analyze.schema import Capture, Classification, WindowInfo


def test_category_values_are_lowercase_stable() -> None:
    assert Category.GAMES == "games"
    assert Category.SCHOOL == "school"
    assert {c.value for c in Category}.isdisjoint({""})


def test_capture_roundtrip() -> None:
    cap = Capture(
        capture_id=1,
        captured_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        active_window=WindowInfo(wm_class="firefox", title="GitHub", pid=1234, active=True),
    )
    dumped = cap.model_dump_json()
    restored = Capture.model_validate_json(dumped)
    assert restored == cap


def test_classification_confidence_bounds() -> None:
    import pytest
    from pydantic import ValidationError

    Classification(capture_id=1, category=Category.GAMES, confidence=0.5, source="heuristic")

    with pytest.raises(ValidationError):
        Classification(capture_id=1, category=Category.GAMES, confidence=1.1, source="heuristic")
    with pytest.raises(ValidationError):
        Classification(capture_id=1, category=Category.GAMES, confidence=-0.1, source="heuristic")
