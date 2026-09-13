"""Кнопка «удалить неподтверждённых» в админке не должна трогать живых людей.

Подтверждение почты «мягкое»: человек входит и работает, даже если письмо не
дошло. Раньше кнопка удаляла всех неподтверждённых старше трёх дней — вместе с
их задачами.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.service import purge_unverified
from app.auth.models import User
from app.tasks.models import Task


async def _user(session: AsyncSession, *, days_old: int, verified: bool = False) -> User:
    user = User(
        id=uuid4(),
        email=f"u-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        created_at=datetime.now(UTC) - timedelta(days=days_old),
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(user)
    await session.commit()
    return user


async def test_unverified_user_with_tasks_survives(db_session: AsyncSession) -> None:
    from app.projects.service import ensure_inbox

    real = await _user(db_session, days_old=10)
    inbox = await ensure_inbox(db_session, real.id)
    db_session.add(Task(user_id=real.id, project_id=inbox.id, title="Алгебра"))
    await db_session.commit()

    bot = await _user(db_session, days_old=10)

    deleted = await purge_unverified(db_session, older_than_days=3)

    emails = {u.email for u in (await db_session.execute(select(User))).scalars()}
    assert deleted == 1
    assert real.email in emails
    assert bot.email not in emails


async def test_verified_and_fresh_are_kept(db_session: AsyncSession) -> None:
    verified = await _user(db_session, days_old=10, verified=True)
    fresh = await _user(db_session, days_old=1)

    assert await purge_unverified(db_session, older_than_days=3) == 0

    emails = {u.email for u in (await db_session.execute(select(User))).scalars()}
    assert {verified.email, fresh.email} <= emails
