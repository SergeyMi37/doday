"""Small presentation helpers exposed to Jinja templates.

Registered as Jinja globals on the template environments that render task rows
(`app/views/router.py` and `app/views/htmx.py`). Pure functions, no DB access.

Пояс берётся из контекста запроса (`timezones.current()`), а не приходит
аргументом: эти функции вызываются из вложенных партиалов, которые
подключаются из полусотни мест, и прокидывать туда пояс через каждый
`{% include %}` значило бы однажды где-нибудь его забыть. Кладётся он один
раз, в `get_current_user`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app import timezones
from app.tasks.models import Task


def due_state(task: Task) -> str:
    """Classify a task's deadline for row styling.

    Returns one of ``"overdue" | "today" | "future" | "none"``. Completed tasks
    never count as overdue/today (their row is already muted), so they fall back
    to ``"future"``. Date-only deadlines compare by calendar day; timed
    deadlines compare against the current instant.
    """
    due = task.due_at
    if due is None:
        return "none"
    if task.is_completed:
        return "future"
    if not task.due_date_only and due < datetime.now(UTC):
        return "overdue"
    day = timezones.due_day(due, date_only=task.due_date_only, tz=timezones.current())
    today = timezones.today_in(timezones.current())
    if day < today:
        return "overdue"
    if day == today:
        return "today"
    return "future"


def due_label(task: Task) -> str:
    """Short, human deadline label for the task-row date chip.

    Yesterday/today/tomorrow get relative words; anything else keeps ``dd.mm``.
    Timed deadlines append ``HH:MM`` — в часах пользователя, а не в UTC.
    Returns "" when there is no due date.
    """
    due = task.due_at
    if due is None:
        return ""
    day = timezones.due_day(due, date_only=task.due_date_only, tz=timezones.current())
    delta = (day - timezones.today_in(timezones.current())).days
    if delta == 0:
        base = "Сегодня"
    elif delta == 1:
        base = "Завтра"
    elif delta == -1:
        base = "Вчера"
    else:
        base = day.strftime("%d.%m")
    if not task.due_date_only:
        base += due.astimezone(timezones.zone_of(timezones.current())).strftime(" %H:%M")
    return base
