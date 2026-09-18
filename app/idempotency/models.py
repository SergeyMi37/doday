"""Таблица ключей идемпотентности: что уже выполняли и чем ответили."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IdempotencyKey(Base):
    """Один запрос — одна строка.

    Строка создаётся ДО выполнения обработчика: она же служит замком. Если
    два одинаковых запроса пришли одновременно (человек нажал дважды, или
    очередь на клиенте отправила повтор раньше, чем пришёл первый ответ),
    вставка второй строки упадёт на уникальном индексе — и второй запрос
    поймёт, что работа уже идёт.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Ключ придумывает клиент. Свой на каждое действие, живёт вместе с
    # неотправленным запросом в очереди, повторяется вместе с ним.
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Метод и путь запоминаем, чтобы поймать чужой ключ: один и тот же ключ
    # на двух разных ручках — это ошибка клиента, а не повтор.
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    # Пока NULL — запрос в работе. Заполнено — есть готовый ответ.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Тело ответа целиком: на повтор отдаём ровно то же, что и в первый раз.
    # У нас это кусок HTML на одну строку задачи — десятки байт.
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
