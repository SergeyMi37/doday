"""Unit tests for the `due_state` template helper (deadline row colouring).

Подписи и раскраска считаются в поясе текущего запроса, поэтому тесты его
задают явно: иначе они молча зависели бы от пояса по умолчанию.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from app import timezones
from app.tasks.models import Task
from app.views.template_filters import due_label, due_state


@pytest.fixture(autouse=True)
def _in_moscow() -> Iterator[None]:
    """Смотрим на строку задачи глазами пользователя из Москвы."""
    timezones.use("Europe/Moscow")
    yield
    timezones.use(None)


def test_no_due_date() -> None:
    assert due_state(Task(due_at=None, due_date_only=True, is_completed=False)) == "none"


def test_overdue_date_only() -> None:
    yesterday = datetime.now(UTC) - timedelta(days=1)
    assert due_state(Task(due_at=yesterday, due_date_only=True, is_completed=False)) == "overdue"


def test_today_date_only() -> None:
    now = datetime.now(UTC)
    assert due_state(Task(due_at=now, due_date_only=True, is_completed=False)) == "today"


def test_future_date_only() -> None:
    later = datetime.now(UTC) + timedelta(days=2)
    assert due_state(Task(due_at=later, due_date_only=True, is_completed=False)) == "future"


def test_completed_never_overdue() -> None:
    yesterday = datetime.now(UTC) - timedelta(days=1)
    assert due_state(Task(due_at=yesterday, due_date_only=True, is_completed=True)) == "future"


def test_overdue_timed() -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    assert due_state(Task(due_at=past, due_date_only=False, is_completed=False)) == "overdue"


def test_today_timed_later() -> None:
    # A timed deadline later today still counts as "today".
    soon = datetime.now(UTC) + timedelta(hours=3)
    state = due_state(Task(due_at=soon, due_date_only=False, is_completed=False))
    assert state in {"today", "future"}  # tolerant near midnight rollover


def test_future_timed() -> None:
    later = datetime.now(UTC) + timedelta(days=2)
    assert due_state(Task(due_at=later, due_date_only=False, is_completed=False)) == "future"


def test_due_label_relative_words() -> None:
    now = datetime.now(UTC)
    assert due_label(Task(due_at=now, due_date_only=True, is_completed=False)) == "Сегодня"
    assert (
        due_label(Task(due_at=now + timedelta(days=1), due_date_only=True, is_completed=False))
        == "Завтра"
    )
    assert (
        due_label(Task(due_at=now - timedelta(days=1), due_date_only=True, is_completed=False))
        == "Вчера"
    )


def test_due_label_absolute_and_timed() -> None:
    far = datetime(2030, 12, 15, tzinfo=UTC)
    # Дата без времени — календарная: 15 декабря остаётся 15 декабря.
    assert due_label(Task(due_at=far, due_date_only=True, is_completed=False)) == "15.12"
    # А срок со временем показывается на часах пользователя: полночь UTC —
    # это три часа ночи по Москве.
    assert due_label(Task(due_at=far, due_date_only=False, is_completed=False)) == "15.12 03:00"
    assert due_label(Task(due_at=None, due_date_only=True, is_completed=False)) == ""


def test_timed_label_follows_the_viewers_zone() -> None:
    """Один и тот же момент — разное время на часах в разных поясах."""
    moment = datetime(2030, 12, 15, 18, 0, tzinfo=UTC)
    task = Task(due_at=moment, due_date_only=False, is_completed=False)
    timezones.use("Europe/Kaliningrad")
    assert due_label(task) == "15.12 20:00"
    timezones.use("Asia/Vladivostok")
    assert due_label(task) == "16.12 04:00"
