from __future__ import annotations

from datetime import datetime, time, timedelta

from familymon.policy.livewarn import (
    DEFAULT_COOLDOWN_MINUTES,
    LiveWarning,
    WarnStore,
    pending_warnings,
    recent_rows,
)
from familymon.policy.schedule import Schedule, ScheduleBlock

# Monday 2026-10-12: Maths 09:20-10:00, English 10:50-11:30 (school);
# a self-study block 10:15-10:45 in between.
SCHED = Schedule(
    blocks=[
        ScheduleBlock("Monday", time(9, 20), time(10, 0), "Maths"),
        ScheduleBlock("Monday", time(10, 15), time(10, 45), "Maths", category="study", notify=True),
        ScheduleBlock("Monday", time(10, 50), time(11, 30), "English"),
    ]
)


def _mon(h, m):
    return datetime(2026, 10, 12, h, m)


def test_warns_once_per_lesson_and_category_with_newest_sighting():
    rows = [
        (_mon(9, 28), "messaging", "WhatsApp - Google Chrome"),
        (_mon(9, 29), "messaging", "WhatsApp - Google Chrome"),
        (_mon(9, 29), "games", "Melon Sandbox | Gameflare.com - Google Chrome"),
        (_mon(9, 29), "browsing_other", "Canvas - Google Chrome"),  # not a distraction
    ]
    due = pending_warnings(SCHED, rows, _mon(9, 30), {})
    assert sorted((w.category, w.seen_at) for w in due) == [
        ("games", _mon(9, 29)),
        ("messaging", _mon(9, 29)),
    ]
    assert all(w.block.subject == "Maths" for w in due)


def test_nothing_outside_a_lesson_even_with_recent_conflicts():
    rows = [(_mon(9, 59), "messaging", None)]
    assert pending_warnings(SCHED, rows, _mon(10, 1), {}) == []  # lesson just ended
    assert pending_warnings(SCHED, rows, _mon(10, 20), {}) == []  # self-study block


def test_rows_from_a_previous_lesson_do_not_warn_in_the_next():
    rows = [(_mon(9, 59), "messaging", None)]
    assert pending_warnings(SCHED, rows, _mon(10, 51), {}) == []


def test_cooldown_suppresses_repeats_then_allows():
    rows = [(_mon(9, 34), "messaging", None)]
    now = _mon(9, 35)
    last = {"09:20|Maths|messaging": now - timedelta(minutes=2)}
    assert pending_warnings(SCHED, rows, now, last) == []
    last = {"09:20|Maths|messaging": now - timedelta(minutes=DEFAULT_COOLDOWN_MINUTES)}
    assert len(pending_warnings(SCHED, rows, now, last)) == 1
    assert len(pending_warnings(SCHED, rows, now, last, cooldown_minutes=10)) == 0


def test_video_warns_only_for_streaming_brand_with_detail():
    stream = [(_mon(9, 30), "video", "Funny cats - YouTube - Google Chrome")]
    (w,) = pending_warnings(SCHED, stream, _mon(9, 31), {})
    assert w.category == "video" and w.detail == "youtube"
    generic = [(_mon(9, 30), "video", "Maths lesson - Adobe Connect")]
    assert pending_warnings(SCHED, generic, _mon(9, 31), {}) == []


def test_message_text():
    w = LiveWarning(block=SCHED.blocks[0], category="messaging", seen_at=_mon(9, 29))
    summary, body = w.message()
    assert summary == "Maths — lesson in progress"
    assert body == "Messaging seen at 09:29 during Maths (until 10:00). This is logged."
    v = LiveWarning(block=SCHED.blocks[0], category="video", seen_at=_mon(9, 29), detail="youtube")
    assert v.message()[1].startswith("Streaming video (youtube) seen at 09:29")


def test_warn_store_roundtrip_and_prunes_other_days(tmp_path):
    p = tmp_path / "warned.json"
    store = WarnStore(p)
    store.mark("09:20|Maths|messaging", _mon(9, 30))
    store.save()
    again = WarnStore(p)
    assert again.last_warned(_mon(9, 30).date()) == {"09:20|Maths|messaging": _mon(9, 30)}
    assert again.last_warned(_mon(9, 30).date() + timedelta(days=1)) == {}
    # Marking on a new day drops yesterday's keys.
    again.mark("09:20|Maths|games", _mon(9, 30) + timedelta(days=1))
    assert set(again.last_warned(_mon(9, 30).date() + timedelta(days=1))) == {"09:20|Maths|games"}
    assert again.last_warned(_mon(9, 30).date()) == {}


def test_warn_store_tolerates_garbage(tmp_path):
    p = tmp_path / "warned.json"
    p.write_text("{nope")
    assert WarnStore(p).last_warned(_mon(9, 30).date()) == {}


def test_recent_rows_joins_classified_captures_only():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from familymon.storage.models import Base, Capture, Classification

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as s:
        old = Capture(captured_at=_mon(9, 0), active_title="old", windows=[])
        new = Capture(captured_at=_mon(9, 29), active_title="WhatsApp - Google Chrome", windows=[])
        unclassified = Capture(captured_at=_mon(9, 29), active_title="x", windows=[])
        s.add_all([old, new, unclassified])
        s.flush()
        for c in (old, new):
            s.add(
                Classification(
                    capture_id=c.id,
                    category="messaging",
                    confidence=0.9,
                    evidence_text="",
                    source="heuristic",
                    created_at=c.captured_at,
                )
            )
        s.commit()
    rows = recent_rows(sessions, _mon(9, 27))
    assert rows == [(_mon(9, 29), "messaging", "WhatsApp - Google Chrome")]


# ---- active window vs. blended verdict ---------------------------------------


def test_warning_category_prefers_the_active_distraction():
    from familymon.policy.livewarn import warning_category

    assert warning_category("school", "messaging") == "messaging"
    assert warning_category("school", "video") == "video"
    assert warning_category("school", "school") == "school"
    assert warning_category("school", "browsing_other") == "school"
    assert warning_category("messaging", None) == "messaging"
    assert warning_category(None, None) is None


def test_active_window_category_sees_the_chat_app_in_front():
    from familymon.analyze.categories import Category
    from familymon.analyze.heuristics import HeuristicClassifier
    from familymon.categorize.apps import AppCategoryLookup
    from familymon.categorize.brands import BrandTitleLookup
    from familymon.categorize.domains import DomainCategoryLookup
    from familymon.policy.livewarn import active_window_category

    apps = AppCategoryLookup()
    apps.add("telegram-desktop", Category.MESSAGING)
    brands = BrandTitleLookup()
    brands.add("interhigh", Category.SCHOOL)
    clf = HeuristicClassifier(apps, DomainCategoryLookup(), brands)
    assert active_window_category(clf, "telegram-desktop", "Telegram (1778)") == "messaging"
    portal = "King's InterHigh - Student portal"
    assert active_window_category(clf, "google-chrome", portal) == "school"
    assert active_window_category(clf, None, None) is None


def test_recent_rows_judges_by_the_active_window_when_given_a_classifier():
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from familymon.analyze.categories import Category
    from familymon.storage.models import Base, Capture, Classification

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as s:
        telegram = Capture(
            captured_at=_mon(9, 29),
            active_wm_class="telegram-desktop",
            active_title="Telegram (3)",
            windows=[],
        )
        s.add(telegram)
        s.flush()
        # The stored verdict is "school": a school page was visible behind it.
        s.add(
            Classification(
                capture_id=telegram.id,
                category="school",
                confidence=0.6,
                evidence_text="",
                source="heuristic",
                created_at=telegram.captured_at,
            )
        )
        s.commit()

    class Stub:
        def classify(self, capture_id, windows, **kw):
            w = windows[0]
            cat = Category.MESSAGING if w.wm_class == "telegram-desktop" else Category.SCHOOL
            return SimpleNamespace(category=cat)

    plain = recent_rows(sessions, _mon(9, 0))
    judged = recent_rows(sessions, _mon(9, 0), Stub())
    assert plain == [(_mon(9, 29), "school", "Telegram (3)")]
    assert judged == [(_mon(9, 29), "messaging", "Telegram (3)")]
