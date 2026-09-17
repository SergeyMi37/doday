"""Часовые пояса: границы суток, переезд, переход на летнее время.

Тесты намеренно не трогают «сейчас»: каждый случай задаёт конкретную дату и
конкретный пояс. Иначе половина из них ломалась бы дважды в год и ещё раз —
когда прогон случайно попадёт на полночь.
"""

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import timezones
from app.auth.models import User
from app.tasks.service import create_task, list_today, list_upcoming, visible_day

# ── разбор имени пояса ──────────────────────────────────────────────────────


def test_unknown_zone_falls_back_instead_of_crashing() -> None:
    """Имя приходит из браузера — значит, прийти может что угодно."""
    assert timezones.normalize("Europe/Mordor") == timezones.DEFAULT_TIMEZONE
    assert timezones.normalize(None) == timezones.DEFAULT_TIMEZONE
    assert timezones.normalize("") == timezones.DEFAULT_TIMEZONE
    assert timezones.normalize("../../etc/passwd") == timezones.DEFAULT_TIMEZONE
    assert timezones.normalize("Asia/Yekaterinburg") == "Asia/Yekaterinburg"


def test_every_offered_zone_exists_in_tzdata() -> None:
    """Список в настройках не должен разъехаться с базой часовых поясов."""
    known = timezones.known_timezones()
    for name, _label in timezones.COMMON_TIMEZONES:
        assert name in known, name


# ── границы суток ───────────────────────────────────────────────────────────


def test_moscow_day_starts_three_hours_earlier_than_utc() -> None:
    start, end = timezones.day_bounds("Europe/Moscow", date(2026, 9, 17))
    assert start == datetime(2026, 9, 16, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 17, 21, 0, tzinfo=UTC)


def test_day_is_23_hours_when_clocks_move_forward() -> None:
    """Лондон, 29 марта 2026: в 01:00 стрелки прыгают на 02:00.

    Если бы конец суток считался как «начало + 24 часа», он уехал бы на час
    вперёд и первый час следующего дня попал бы во вчерашний список.
    """
    start, end = timezones.day_bounds("Europe/London", date(2026, 3, 29))
    assert end - start == timedelta(hours=23)
    assert end == timezones.day_start("Europe/London", date(2026, 3, 30))


def test_day_is_25_hours_when_clocks_move_back() -> None:
    """Сантьяго, 4 апреля 2026 — сутки, наоборот, длиннее."""
    start, end = timezones.day_bounds("America/Santiago", date(2026, 4, 4))
    assert end - start == timedelta(hours=25)


def test_local_date_of_treats_naive_as_utc() -> None:
    naive = datetime(2026, 9, 17, 22, 30)
    assert timezones.local_date_of(naive, "Europe/Moscow") == date(2026, 9, 18)
    assert timezones.local_date_of(naive, "UTC") == date(2026, 9, 17)


# ── два вида срока ──────────────────────────────────────────────────────────


def test_timed_deadline_moves_with_the_person() -> None:
    """Момент времени остаётся моментом: меняется только то, что на часах."""
    moment = datetime(2026, 9, 17, 22, 0, tzinfo=UTC)
    assert timezones.due_day(moment, date_only=False, tz="UTC") == date(2026, 9, 17)
    assert timezones.due_day(moment, date_only=False, tz="Europe/Moscow") == date(2026, 9, 18)


def test_date_only_deadline_does_not_move() -> None:
    """«Сдать в четверг» — это четверг везде, от Калининграда до Камчатки."""
    thursday = datetime(2026, 9, 17, 23, 59, tzinfo=UTC)
    for tz in ("UTC", "Europe/Moscow", "Asia/Kamchatka", "America/Los_Angeles"):
        assert timezones.due_day(thursday, date_only=True, tz=tz) == date(2026, 9, 17)


# ── списки задач ────────────────────────────────────────────────────────────


async def _user(db_session: AsyncSession, tz: str | None = None, email: str = "tz@s.ru") -> User:
    u = User(email=email, password_hash="argon2-fake", timezone=tz)
    db_session.add(u)
    await db_session.commit()
    return u


async def test_late_evening_task_belongs_to_tomorrow_in_moscow(db_session: AsyncSession) -> None:
    """22:00 UTC — это уже час ночи следующего дня по Москве.

    Для UTC-пользователя задача сегодняшняя, для московского — завтрашняя.
    Один и тот же ряд в базе, разные ответы: это и значит «сутки считаются в
    поясе человека».
    """
    user = await _user(db_session)
    tomorrow_utc_evening = datetime.combine(
        timezones.today_in("UTC"), datetime.min.time(), tzinfo=UTC
    ) + timedelta(hours=22)
    await create_task(
        db_session,
        user.id,
        title="Поздний созвон",
        due_at=tomorrow_utc_evening,
        due_date_only=False,
    )

    in_utc = await list_today(db_session, user.id, tz="UTC")
    in_moscow = await list_today(db_session, user.id, tz="Europe/Moscow")

    assert [t.title for t in in_utc] == ["Поздний созвон"]
    assert in_moscow == []


async def test_upcoming_window_counts_days_not_hours(db_session: AsyncSession) -> None:
    """Неделя вперёд — это семь календарных дней, а не 168 часов."""
    user = await _user(db_session)
    tz = "Europe/Moscow"
    today = timezones.today_in(tz)
    for offset in (1, 7, 8):
        await create_task(
            db_session,
            user.id,
            title=f"через {offset}",
            due_at=timezones.floating_start(today + timedelta(days=offset)),
        )

    titles = {t.title for t in await list_upcoming(db_session, user.id, days=7, tz=tz)}
    assert titles == {"через 1", "через 7"}


async def test_relocation_changes_the_day_without_touching_the_task(
    db_session: AsyncSession,
) -> None:
    """Человек переехал — задача осталась той же, а день сменился.

    Ровно то, чего не умела первая версия: пояс хранился нигде, и «Сегодня»
    у переехавшего оставалось московским.
    """
    user = await _user(db_session, tz="Europe/Moscow")
    moment = datetime(2026, 9, 17, 20, 0, tzinfo=UTC)  # 23:00 в Москве, 07:00 в Магадане 18-го
    task = await create_task(
        db_session, user.id, title="Тренировка", due_at=moment, due_date_only=False
    )

    assert visible_day(task, "Europe/Moscow") == date(2026, 9, 17)
    user.timezone = "Asia/Magadan"
    await db_session.commit()
    assert visible_day(task, user.timezone) == date(2026, 9, 18)


async def test_date_only_task_survives_relocation(db_session: AsyncSession) -> None:
    """А вот «сдать в четверг» при переезде обязано остаться четвергом."""
    user = await _user(db_session, tz="Europe/Kaliningrad")
    task = await create_task(
        db_session,
        user.id,
        title="Реферат",
        due_at=datetime(2026, 9, 17, 23, 59, tzinfo=UTC),
        due_date_only=True,
    )
    assert visible_day(task, "Europe/Kaliningrad") == date(2026, 9, 17)
    assert visible_day(task, "Asia/Kamchatka") == date(2026, 9, 17)


# ── серия выполненных дней ──────────────────────────────────────────────────


async def test_streak_counts_the_local_day(db_session: AsyncSession) -> None:
    """Задача, закрытая в полночь по Москве, — это сегодня, а не завтра.

    В UTC такая отметка попадала в следующие сутки: серия рвалась у всех, кто
    работает по вечерам.
    """
    from app.stats.service import current_streak

    user = await _user(db_session, tz="Europe/Moscow")
    task = await create_task(db_session, user.id, title="Домашка")
    task.is_completed = True
    # 21:30 UTC = 00:30 следующего дня по Москве.
    task.completed_at = timezones.day_end("Europe/Moscow", timezones.today_in("Europe/Moscow"))
    task.completed_at -= timedelta(minutes=30)
    await db_session.commit()

    assert await current_streak(db_session, user.id, tz="Europe/Moscow") == 1


async def test_streak_badge_and_stats_page_agree(db_session: AsyncSession) -> None:
    """Цифра в шапке и цифра на странице статистики считаются одним кодом."""
    from app.stats.service import compute_user_stats, streak_summary

    user = await _user(db_session, tz="Asia/Vladivostok")
    task = await create_task(db_session, user.id, title="Задача")
    task.is_completed = True
    task.completed_at = datetime.now(UTC)
    await db_session.commit()

    badge = await streak_summary(db_session, user.id, tz=user.timezone)
    page = await compute_user_stats(db_session, user.id, tz=user.timezone)
    assert badge["current"] == page["current_streak"]
    assert badge["longest"] == page["longest_streak"]


# ── утренний дайджест ───────────────────────────────────────────────────────


async def test_digest_goes_out_at_seven_in_the_local_zone(db_session: AsyncSession) -> None:
    """Крон приходит раз в час; письмо получает тот, у кого сейчас утро."""
    from app.digest.service import send_morning_digests_for_all_users

    user = await _user(db_session, tz="Asia/Yekaterinburg")
    user.email_verified_at = datetime.now(UTC)
    user.morning_digest_enabled = True
    user.tier = "pro"
    user.pro_until = datetime.now(UTC) + timedelta(days=30)
    await create_task(db_session, user.id, title="Что-то на сегодня")
    await db_session.commit()

    # 07:00 в Екатеринбурге — это 02:00 UTC.
    too_early = await send_morning_digests_for_all_users(
        db_session, now=datetime(2026, 9, 17, 4, 0, tzinfo=UTC)
    )
    assert too_early["sent"] == 0
    assert too_early["skipped_not_morning"] == 1


# ── настройка пояса ─────────────────────────────────────────────────────────


async def test_browser_reports_the_zone_and_it_sticks(
    logged_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    r = await logged_in_client.post(
        "/api/profile/timezone", data={"timezone": "Asia/Novosibirsk", "auto": "true"}
    )
    assert r.status_code == 200
    assert r.json() == {"timezone": "Asia/Novosibirsk", "auto": True}


async def test_garbage_zone_is_rejected(logged_in_client: AsyncClient) -> None:
    r = await logged_in_client.post(
        "/api/profile/timezone", data={"timezone": "Europe/Mordor", "auto": "true"}
    )
    assert r.status_code == 422


async def test_manual_choice_is_not_overwritten_by_the_browser(
    logged_in_client: AsyncClient,
) -> None:
    """Выбрал руками — значит, автоопределение больше не вмешивается."""
    await logged_in_client.post(
        "/api/profile/timezone", data={"timezone": "Asia/Yakutsk", "auto": "false"}
    )
    r = await logged_in_client.post(
        "/api/profile/timezone", data={"timezone": "Europe/Moscow", "auto": "true"}
    )
    assert r.json() == {"timezone": "Asia/Yakutsk", "auto": False}


async def test_switching_back_to_auto_lets_the_browser_win(
    logged_in_client: AsyncClient,
) -> None:
    await logged_in_client.post(
        "/api/profile/timezone", data={"timezone": "Asia/Yakutsk", "auto": "false"}
    )
    await logged_in_client.post("/api/profile/timezone", data={"timezone": "auto", "auto": "false"})
    r = await logged_in_client.post(
        "/api/profile/timezone", data={"timezone": "Europe/Samara", "auto": "true"}
    )
    assert r.json() == {"timezone": "Europe/Samara", "auto": True}


async def test_settings_page_offers_the_zone(logged_in_client: AsyncClient) -> None:
    html = (await logged_in_client.get("/app/settings")).text
    assert "Часовой пояс" in html
    assert 'value="Asia/Yekaterinburg"' in html


async def test_every_page_asks_the_browser_for_the_zone(logged_in_client: AsyncClient) -> None:
    """Без этого куска пояс не появится сам, и всё остальное бессмысленно."""
    html = (await logged_in_client.get("/app/today")).text
    assert "resolvedOptions().timeZone" in html
    assert "/api/profile/timezone" in html
