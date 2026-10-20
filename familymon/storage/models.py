from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Capture(Base):
    __tablename__ = "capture"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    active_wm_class: Mapped[str | None] = mapped_column(String(256))
    active_title: Mapped[str | None] = mapped_column(String(512))
    active_pid: Mapped[int | None] = mapped_column(Integer)
    active_exe: Mapped[str | None] = mapped_column(String(512))

    windows: Mapped[list[dict]] = mapped_column(JSON, default=list)

    screenshot_path: Mapped[str | None] = mapped_column(String(512))
    screenshot_phash: Mapped[str | None] = mapped_column(String(32), index=True)
    screenshot_skipped_reason: Mapped[str | None] = mapped_column(String(64))
    # A messaging window was visible this tick (redacted or full-skipped).
    messaging_present: Mapped[bool] = mapped_column(Boolean, default=False)
    # The persisted screenshot has messaging regions blacked out.
    screenshot_redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    # A visible window belongs to an installed game (trusted detection).
    game_running: Mapped[bool] = mapped_column(Boolean, default=False)
    # Captured inside the parent's personal-time window (policy
    # `personal_time`): no screenshot was taken, and the dashboard shows the
    # period as one block without titles or categories. Classification and
    # game-time charging still run on the row.
    personal_time: Mapped[bool] = mapped_column(Boolean, default=False)

    same_as_prev: Mapped[bool] = mapped_column(Boolean, default=False)

    classification: Mapped[Classification | None] = relationship(
        back_populates="capture",
        uselist=False,
        cascade="all, delete-orphan",
    )


class Classification(Base):
    __tablename__ = "classification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    capture_id: Mapped[int] = mapped_column(
        ForeignKey("capture.id", ondelete="CASCADE"),
        unique=True,
    )

    category: Mapped[str] = mapped_column(String(32), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_text: Mapped[str | None] = mapped_column(String(1024))
    source: Mapped[str] = mapped_column(String(32), index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    # Distinct categories among visible windows this tick (incl. the primary),
    # priority-ordered. The "also-present" half of the hybrid classification.
    present_categories: Mapped[list[str]] = mapped_column(JSON, default=list)

    raw_response: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    capture: Mapped[Capture] = relationship(back_populates="classification")

    __table_args__ = (Index("ix_classification_category_created", "category", "created_at"),)


class Event(Base):
    """Operational events: heartbeats, capture errors, etc.

    Kept out of `Capture` to keep that table append-only-per-tick and small.
    """

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict | None] = mapped_column(JSON)
