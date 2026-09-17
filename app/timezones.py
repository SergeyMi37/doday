"""Часовые пояса пользователей: единственное место, где считается «день».

Зачем отдельный модуль. В базе все моменты времени лежат в UTC — это
правильно и менять не нужно. Но всё, что человек видит, живёт не в UTC:
«Сегодня», «Ближайшие», серия выполненных дней, утренний дайджест, разбор
«завтра в 18:00» из быстрого ввода. Пока границы суток считались в UTC,
у московского пользователя день заканчивался в три часа ночи, а задача,
поставленная на 01:00 понедельника, показывалась в воскресном списке.

Поэтому правило одно: **в базу пишем UTC, границы суток считаем в поясе
пользователя**. Все функции ниже возвращают либо локальную дату, либо
границы локальных суток, уже переведённые в UTC, — так их можно прямо
подставлять в запросы, ничего не пересчитывая на месте.

Про переход на летнее время. Сутки не всегда длятся 24 часа: в день
перехода их 23 или 25. Поэтому начало завтрашних суток нельзя получить
прибавлением суток к началу сегодняшних — только через календарную дату.
Именно так и сделано в `day_bounds`, и на это есть тесты (Лондон, Сантьяго).
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy import ColumnExpressionArgument, func
from sqlalchemy.sql.elements import ColumnElement

# Аудитория преимущественно российская, поэтому пояс по умолчанию московский,
# а не UTC: до того, как браузер пришлёт настоящий, лучше ошибиться на час для
# Екатеринбурга, чем на три для всех.
DEFAULT_TIMEZONE = "Europe/Moscow"

# Показываем в настройках коротким списком — полный перечень из четырёхсот с
# лишним имён выбирать невозможно. Пояс всё равно определяется автоматически,
# список нужен тем, кто хочет поставить руками.
COMMON_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("Europe/Kaliningrad", "Калининград — MSK−1"),
    ("Europe/Moscow", "Москва, Санкт-Петербург — MSK"),
    ("Europe/Samara", "Самара, Ижевск — MSK+1"),
    ("Asia/Yekaterinburg", "Екатеринбург, Пермь — MSK+2"),
    ("Asia/Omsk", "Омск — MSK+3"),
    ("Asia/Krasnoyarsk", "Красноярск, Новокузнецк — MSK+4"),
    ("Asia/Irkutsk", "Иркутск, Улан-Удэ — MSK+5"),
    ("Asia/Yakutsk", "Якутск, Чита — MSK+6"),
    ("Asia/Vladivostok", "Владивосток, Хабаровск — MSK+7"),
    ("Asia/Magadan", "Магадан, Сахалин — MSK+8"),
    ("Asia/Kamchatka", "Камчатка, Чукотка — MSK+9"),
    ("Europe/Minsk", "Минск — MSK"),
    ("Asia/Almaty", "Алматы — MSK+3"),
    ("Asia/Tbilisi", "Тбилиси — MSK+1"),
    ("Asia/Yerevan", "Ереван — MSK+1"),
    ("Europe/Kyiv", "Киев — MSK−1"),
    ("Europe/Belgrade", "Белград, Будапешт — MSK−2"),
    ("Asia/Dubai", "Дубай — MSK+1"),
    ("Asia/Bangkok", "Бангкок — MSK+4"),
    ("UTC", "UTC — всемирное время"),
)


# Пояс текущего запроса — только для слоя отображения.
#
# Правило, которое здесь важно не размыть: сервисы и запросы к базе получают
# пояс явным аргументом `tz`, потому что их вызывают не только из HTTP (ещё
# из бота и из cron, где никакого «текущего пользователя» нет). А вот
# шаблонным фильтрам вроде «Сегодня / Завтра / просрочено» аргумент прокинуть
# негде: они вызываются из вложенных партиалов, которые подключаются из
# полусотни мест. Для них пояс кладётся в контекст запроса один раз — в
# `get_current_user`, где пользователь и так загружается.
#
# У каждого запроса свой контекст (asyncio копирует его при создании задачи),
# так что чужой пояс сюда не протечёт.
_CURRENT: ContextVar[str] = ContextVar("current_timezone", default=DEFAULT_TIMEZONE)


def use(name: str | None) -> None:
    """Запомнить пояс текущего запроса. Вызывается один раз, на входе."""
    _CURRENT.set(normalize(name))


def current() -> str:
    """Пояс текущего запроса. Для анонимных — пояс по умолчанию."""
    return _CURRENT.get()


@lru_cache(maxsize=512)
def _zone(name: str) -> ZoneInfo | None:
    """Разобрать имя пояса. None, если такого нет в базе tzdata."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def is_valid(name: str | None) -> bool:
    """Существует ли такой пояс. Имена приходят от браузера — доверять нельзя."""
    return bool(name) and _zone(str(name)) is not None


def normalize(name: str | None) -> str:
    """Привести к рабочему имени пояса: чужое или пустое заменить на наше."""
    return str(name) if is_valid(name) else DEFAULT_TIMEZONE


def zone_of(name: str | None) -> ZoneInfo:
    """Пояс пользователя. Неизвестное имя не роняет запрос, а откатывается."""
    zone = _zone(normalize(name))
    # normalize уже вернула проверенное имя, но mypy этого не знает.
    return zone if zone is not None else ZoneInfo(DEFAULT_TIMEZONE)


def now_in(name: str | None) -> datetime:
    """Текущий момент глазами пользователя."""
    return datetime.now(zone_of(name))


def today_in(name: str | None) -> date:
    """Какое сегодня число у пользователя. Не то же самое, что в UTC."""
    return now_in(name).date()


def local_date_of(moment: datetime, name: str | None) -> date:
    """В какой локальный день попадает момент времени.

    Наивный datetime считаем UTC: в базе всё хранится в UTC, но драйвер в
    отдельных местах отдаёт значения без пояса.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone_of(name)).date()


def day_bounds(name: str | None, day: date | None = None) -> tuple[datetime, datetime]:
    """Границы локальных суток в UTC: [начало дня, начало следующего дня).

    Возвращаем полуинтервал, а не «до 23:59:59»: иначе задача со сроком
    23:59:59.5 не попадает никуда.

    Начало следующего дня берём от календарной даты, а не прибавлением
    24 часов: в день перехода на летнее время сутки короче или длиннее.
    """
    zone = zone_of(name)
    if day is None:
        day = datetime.now(zone).date()
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def day_start(name: str | None, day: date | None = None) -> datetime:
    """Начало локальных суток в UTC."""
    return day_bounds(name, day)[0]


def day_end(name: str | None, day: date | None = None) -> datetime:
    """Начало следующих локальных суток в UTC — верхняя граница полуинтервала."""
    return day_bounds(name, day)[1]


def local_hour_sql(column: ColumnExpressionArgument[Any], name: str | None) -> ColumnElement[int]:
    """SQL-выражение «который был час на часах пользователя».

    Нужно достижениям вроде «жаворонка»: «закрыл пять задач до девяти утра»
    имеет смысл только в местных часах. Раньше там стоял час по UTC, и для
    Москвы «до девяти утра» на деле означало «до полудня».
    """
    return cast(
        "ColumnElement[int]",
        func.extract("hour", func.timezone(normalize(name), column)),
    )


def floating_start(day: date) -> datetime:
    """Начало календарной даты для «плавающих» сроков — полночь UTC.

    У задачи два разных вида срока, и путать их нельзя.

    «Позвонить в 18:00» — это момент времени. Он привязан к точке на шкале:
    если человек переехал, момент остался прежним, поменялось только то, как
    он выглядит на часах. Такие сроки сравниваются с границами суток из
    `day_bounds`.

    «Сдать реферат в четверг» — это календарная дата. Никакого момента за ней
    нет: четверг остаётся четвергом и в Москве, и во Владивостоке. Хранится
    такая дата как момент внутри этих суток по UTC (быстрый ввод кладёт 23:59,
    форма — полночь), и ехать вместе с человеком она не должна: иначе при
    переезде на восток все дедлайны «на день» разом сдвинулись бы на сутки.

    Отсюда правило: моменты сравниваем в поясе пользователя, календарные даты
    — всегда в UTC, как чистые даты.
    """
    return datetime.combine(day, time.min, tzinfo=UTC)


def due_day(due: datetime, *, date_only: bool, tz: str | None) -> date:
    """В какой день пользователь видит этот срок.

    Единственная функция, которая знает про разницу двух видов срока, — всё
    остальное (группировка по дням, подписи «Сегодня/Завтра», раскраска
    просроченных) спрашивает день здесь.
    """
    if date_only:
        return due.astimezone(UTC).date()
    return local_date_of(due, tz)


def known_timezones() -> set[str]:
    """Все имена из базы tzdata — для проверки в тестах и в настройках."""
    return available_timezones()


def local_date_sql(column: ColumnExpressionArgument[Any], name: str | None) -> ColumnElement[date]:
    """SQL-выражение «календарная дата этого момента в поясе пользователя».

    Нужно там, где день считает база: `GROUP BY` по дням для тепловой карты,
    подсчёт серий, «сколько сделано сегодня». Раньше там стояло
    `date(completed_at)`, а соединение с базой живёт в UTC — значит, дата
    получалась тоже в UTC.

    `timezone('Europe/Moscow', ts)` в Postgres переводит момент в локальное
    время пояса, и уже от него берётся дата. Postgres знает тот же tzdata,
    что и Python, поэтому переходы на летнее время учитываются одинаково с
    обеих сторон — расхождения между тем, что посчитал питон, и тем, что
    посчитала база, не будет.
    """
    return cast(
        "ColumnElement[date]",
        func.date(func.timezone(normalize(name), column)),
    )
