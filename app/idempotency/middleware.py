"""Перехват повторных запросов по заголовку `Idempotency-Key`.

Сделано middleware, а не зависимостью роутера, потому что нужен готовый
ответ: зависимость видит запрос до обработчика и не может подменить его
результат. Здесь же ответ проходит через нас целиком — и на повтор мы отдаём
ровно тот, что был в первый раз.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_maker
from app.idempotency.models import IdempotencyKey, _utcnow

_log = structlog.get_logger(__name__)

HEADER = "Idempotency-Key"
_GUARDED_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
# Ответы у нас — кусочки HTML на одну строку списка. Мегабайтные тела сюда
# попадать не должны; если попадут, лучше не хранить вовсе, чем раздувать
# таблицу. Клиент в этом случае получит на повтор пустой 200 — неприятно, но
# честнее, чем молча выполнить работу второй раз.
MAX_STORED_BODY = 64 * 1024


def _user_id(request: Request) -> UUID | None:
    """Чей это запрос. Ключи у каждого свои: чужой ключ подобрать нельзя."""
    raw = request.session.get("user_id")
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


async def _stored_response(session: AsyncSession, row: IdempotencyKey) -> Response:
    """Повтор: отдаём тот же ответ, помечая, что это не новая работа."""
    return Response(
        content=row.body or "",
        status_code=row.status_code or 200,
        media_type="text/html; charset=utf-8",
        headers={"Idempotency-Replayed": "1"},
    )


async def idempotency_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    key = request.headers.get(HEADER, "").strip()
    if not key or request.method not in _GUARDED_METHODS or len(key) > 80:
        return await call_next(request)

    user_id = _user_id(request)
    if user_id is None:
        # Анонимному хранить ключи негде и незачем: за ним не закреплено
        # ничего, что можно было бы задвоить.
        return await call_next(request)

    path = request.url.path[:300]
    maker = get_session_maker()

    async with maker() as session:
        # Вставка — это и есть взятие замка. Уникальный индекс (user_id, key)
        # гарантирует, что строку создаст ровно один запрос, даже если их
        # прилетело три штуки одновременно.
        inserted = await session.execute(
            pg_insert(IdempotencyKey)
            .values(key=key, user_id=user_id, method=request.method, path=path)
            .on_conflict_do_nothing(index_elements=["user_id", "key"])
            .returning(IdempotencyKey.id)
        )
        row_id = inserted.scalar_one_or_none()
        await session.commit()

        if row_id is None:
            existing = (
                await session.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
                    )
                )
            ).scalar_one_or_none()
            if existing is None:  # pragma: no cover — ключ удалили между запросами
                return await call_next(request)
            if existing.method != request.method or existing.path != path:
                return JSONResponse(
                    {"detail": "Этот Idempotency-Key уже использован для другого запроса"},
                    status_code=422,
                )
            if existing.completed_at is None:
                # Первый запрос ещё в работе. Просить клиента повторить позже
                # честнее, чем выполнять вторую копию той же работы.
                return JSONResponse(
                    {"detail": "Запрос с этим ключом ещё выполняется"},
                    status_code=409,
                    headers={"Retry-After": "2"},
                )
            return await _stored_response(session, existing)

    response = await call_next(request)
    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]

    async with maker() as session:
        if response.status_code >= 500:
            # Сервер упал — работа, скорее всего, не сделана. Ключ убираем,
            # чтобы повтор был настоящим повтором, а не «уже выполнено».
            row = (
                await session.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)
                await session.commit()
            _log.info("idempotency_key_released", path=path, status=response.status_code)
        else:
            stored = body.decode("utf-8", "replace") if len(body) <= MAX_STORED_BODY else ""
            row = (
                await session.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.status_code = response.status_code
                row.body = stored
                row.completed_at = _utcnow()
                await session.commit()

    return Response(
        content=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )
