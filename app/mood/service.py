"""Mood-tracker business logic — upsert by date, last-N-days summary."""

from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import timezones
from app.mood.models import MoodEntry


async def upsert_mood(
    session: AsyncSession,
    user_id: UUID,
    *,
    on_date: date | None = None,
    score: int,
    note: str | None = None,
    tz: str | None = None,
) -> MoodEntry:
    if not 1 <= score <= 5:
        raise ValueError("score must be in 1..5")
    target = on_date or timezones.today_in(tz)
    existing = (
        await session.execute(
            select(MoodEntry).where(MoodEntry.user_id == user_id, MoodEntry.mood_date == target)
        )
    ).scalar_one_or_none()
    if existing is None:
        entry = MoodEntry(user_id=user_id, mood_date=target, score=score, note=note or None)
        session.add(entry)
    else:
        existing.score = score
        if note is not None:
            existing.note = note or None
        entry = existing
    await session.commit()
    await session.refresh(entry)
    return entry


async def history(
    session: AsyncSession, user_id: UUID, *, days: int = 30, tz: str | None = None
) -> list[MoodEntry]:
    horizon = timezones.today_in(tz) - timedelta(days=days)
    rows = await session.execute(
        select(MoodEntry)
        .where(MoodEntry.user_id == user_id, MoodEntry.mood_date >= horizon)
        .order_by(MoodEntry.mood_date.desc())
    )
    return list(rows.scalars().all())


async def get_today(
    session: AsyncSession, user_id: UUID, *, tz: str | None = None
) -> MoodEntry | None:
    today = timezones.today_in(tz)
    return (
        await session.execute(
            select(MoodEntry).where(MoodEntry.user_id == user_id, MoodEntry.mood_date == today)
        )
    ).scalar_one_or_none()
