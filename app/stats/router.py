"""Lightweight JSON endpoints for stats badges shown across the UI."""

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter
from sqlalchemy import func, select

from app.auth.deps import DbSession, RequiredUser
from app.school.subjects import detect_subject
from app.stats.service import streak_summary
from app.tasks.models import Task

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/streak")
async def streak(user: RequiredUser, session: DbSession) -> dict[str, int | bool]:
    """Return the current + longest completion streak. Used by the topbar chip.

    Арифметика — в app.stats.service: здесь лежала её третья копия, которая
    вдобавок считала дни в UTC, поэтому бейдж в шапке и цифра на странице
    статистики иногда расходились на единицу.
    """
    return await streak_summary(session, user.id, tz=user.timezone)


def _longest(days: set[date]) -> int:
    if not days:
        return 0
    sorted_days = sorted(days)
    longest = 1
    run = 1
    for i in range(1, len(sorted_days)):
        if (sorted_days[i] - sorted_days[i - 1]).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    return longest


def _streak_from_days(days_set: set[date], today: date) -> dict[str, int | bool]:
    if not days_set:
        return {"current": 0, "longest": 0, "today_done": False}
    if today in days_set:
        cursor: date | None = today
    elif (today - timedelta(days=1)) in days_set:
        cursor = today - timedelta(days=1)
    else:
        cursor = None
    current = 0
    if cursor is not None:
        while cursor in days_set:
            current += 1
            cursor -= timedelta(days=1)
    return {
        "current": current,
        "longest": max(current, _longest(days_set)),
        "today_done": today in days_set,
    }


@router.get("/school-streak")
async def school_streak(user: RequiredUser, session: DbSession) -> dict[str, int | bool]:
    """Streak of consecutive days with at least one completed *school* task.

    A task counts as a school task if its title mentions a known subject
    (см. `app.school.subjects.detect_subject`).
    """
    today = datetime.now(UTC).date()
    horizon = today - timedelta(days=400)
    rows = await session.execute(
        select(Task.title, func.date(Task.completed_at)).where(
            Task.user_id == user.id,
            Task.is_completed.is_(True),
            Task.completed_at.is_not(None),
            func.date(Task.completed_at) >= horizon,
        )
    )
    days_set: set[date] = set()
    for title, d_raw in rows.all():
        if d_raw is None or detect_subject(title) is None:
            continue
        d = d_raw if isinstance(d_raw, date) else date.fromisoformat(str(d_raw))
        days_set.add(d)
    return _streak_from_days(days_set, today)
