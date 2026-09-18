"""Повтор запроса не должен делать работу дважды.

Проверяем то, ради чего всё затевалось: клиент потерял ответ, повторил
запрос — и в списке не появилось второй такой же задачи.
"""

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import Task


async def _task_count(session: AsyncSession, title: str) -> int:
    return int(
        (
            await session.execute(select(func.count()).select_from(Task).where(Task.title == title))
        ).scalar_one()
    )


# ── ключ идемпотентности ────────────────────────────────────────────────────


async def test_repeat_with_same_key_creates_one_task(
    logged_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Главный случай: сеть пропала, клиент повторил — задача одна."""
    payload = {"text": "Купить тетрадь"}
    headers = {"Idempotency-Key": "key-abc-123"}

    first = await logged_in_client.post("/htmx/quickadd", data=payload, headers=headers)
    second = await logged_in_client.post("/htmx/quickadd", data=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    # Ответ на повтор — тот же самый, а не новый.
    assert second.text == first.text
    assert second.headers.get("Idempotency-Replayed") == "1"
    assert await _task_count(db_session, "Купить тетрадь") == 1


async def test_different_keys_create_two_tasks(
    logged_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Человек действительно добавил две одинаковые задачи — не мешаем."""
    payload = {"text": "Позвонить бабушке"}

    await logged_in_client.post("/htmx/quickadd", data=payload, headers={"Idempotency-Key": "k-1"})
    await logged_in_client.post("/htmx/quickadd", data=payload, headers={"Idempotency-Key": "k-2"})

    assert await _task_count(db_session, "Позвонить бабушке") == 2


async def test_without_key_nothing_changes(
    logged_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Старые страницы ключей не шлют — они должны работать как прежде."""
    payload = {"text": "Полить цветы"}
    await logged_in_client.post("/htmx/quickadd", data=payload)
    await logged_in_client.post("/htmx/quickadd", data=payload)

    assert await _task_count(db_session, "Полить цветы") == 2


async def test_same_key_on_another_endpoint_is_rejected(logged_in_client: AsyncClient) -> None:
    """Один ключ на два разных запроса — ошибка клиента, а не повтор."""
    headers = {"Idempotency-Key": "reused-key"}
    await logged_in_client.post("/htmx/quickadd", data={"text": "Первая"}, headers=headers)
    r = await logged_in_client.post(
        "/htmx/sections",
        data={"project_id": "00000000-0000-0000-0000-000000000000"},
        headers=headers,
    )
    assert r.status_code == 422


async def test_key_is_not_shared_between_users(
    logged_in_client: AsyncClient,
    second_logged_in_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Ключи у каждого свои: чужой подобрать нельзя."""
    headers = {"Idempotency-Key": "same-string"}
    await logged_in_client.post("/htmx/quickadd", data={"text": "Общая"}, headers=headers)
    await second_logged_in_client.post("/htmx/quickadd", data={"text": "Общая"}, headers=headers)

    assert await _task_count(db_session, "Общая") == 2


# ── смысловая идемпотентность: состояние вместо переключателя ──────────────


async def test_repeating_complete_keeps_task_done(
    logged_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Повтор «сделай выполненной» оставляет задачу выполненной.

    Ровно здесь ломался старый /toggle: второй запрос возвращал задачу
    обратно в невыполненные, то есть отменял действие человека.
    """
    create = await logged_in_client.post("/htmx/quickadd", data={"text": "Сдать эссе"})
    assert create.status_code == 200
    task = (await db_session.execute(select(Task).where(Task.title == "Сдать эссе"))).scalar_one()

    for _ in range(3):
        r = await logged_in_client.post(f"/htmx/tasks/{task.id}/state", data={"completed": "true"})
        assert r.status_code == 200

    await db_session.refresh(task)
    assert task.is_completed is True


async def test_repeating_complete_does_not_duplicate_recurring_task(
    logged_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """У повторяющейся задачи повтор запроса не плодит будущие копии."""
    await logged_in_client.post("/htmx/quickadd", data={"text": "Тренировка каждый понедельник"})
    task = (
        (await db_session.execute(select(Task).where(Task.recurrence.is_not(None))))
        .scalars()
        .first()
    )
    assert task is not None, "быстрый ввод должен был распознать повтор"
    assert task.due_at is not None, "у повтора должна быть первая дата"

    before = int((await db_session.execute(select(func.count()).select_from(Task))).scalar_one())
    for _ in range(3):
        await logged_in_client.post(f"/htmx/tasks/{task.id}/state", data={"completed": "true"})
    after = int((await db_session.execute(select(func.count()).select_from(Task))).scalar_one())

    # Ровно одна новая задача — следующий повтор, а не три.
    assert after - before == 1


async def test_uncomplete_is_explicit(
    logged_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    await logged_in_client.post("/htmx/quickadd", data={"text": "Вернуть книгу"})
    task = (
        await db_session.execute(select(Task).where(Task.title == "Вернуть книгу"))
    ).scalar_one()

    await logged_in_client.post(f"/htmx/tasks/{task.id}/state", data={"completed": "true"})
    await logged_in_client.post(f"/htmx/tasks/{task.id}/state", data={"completed": "false"})
    await logged_in_client.post(f"/htmx/tasks/{task.id}/state", data={"completed": "false"})

    await db_session.refresh(task)
    assert task.is_completed is False
