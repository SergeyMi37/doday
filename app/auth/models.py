"""SQLAlchemy ORM models for the auth feature."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _trial_default() -> datetime:
    return datetime.now(UTC) + timedelta(days=14)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    # Nullable since 2026-05-25 (migration 0041): Lessio-юзеры заходят только
    # через Telegram WebApp initData, без пароля. password_hash остаётся NULL.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="free")
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=_trial_default
    )
    # Long-lived per-user token for unauthenticated calendar feeds (.ics). Lazy
    # — generated on first /api/calendar/feed request. Rotatable via /profile.
    ical_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    # Morning digest opt-in. False by default — explicit opt-in via /profile.
    # Cron worker shells `last_sent_at` after each successful send to dedupe.
    morning_digest_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    morning_digest_last_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # True for the operator account (yarik@doday.app on prod). Gives access to
    # /app/root admin panel, /api/admin/* endpoints, and complaint management.
    # Set via SQL/migration, never via UI — no risk of self-promotion.
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Per-user opt-in flags for experimental features (revived/in-progress).
    # Shape: {"graph": true, "habits": false, ...}. Defaults to empty dict so
    # experimental features stay OFF unless the user enables them in settings.
    experiments: Mapped[dict[str, bool]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # When the user's paid Pro subscription expires (NULL = no paid sub, ever
    # or already lapsed long ago). effective_tier() returns "pro" while this
    # is in the future; otherwise falls back to trial / free. Payment via
    # Telegram Stars extends this — see app/billing/stars.py.
    pro_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Адрес и подсеть, с которых пришла регистрация. Нужны, чтобы считать
    # частоту регистраций по БД, а не по счётчику в памяти: деплой
    # перезапускает процесс и обнулял бы защиту.
    signup_ip: Mapped[str | None] = mapped_column(String(45), nullable=True, index=True)
    signup_subnet: Mapped[str | None] = mapped_column(String(45), nullable=True, index=True)
    # Часовой пояс пользователя в виде имени IANA («Europe/Moscow»). По нему
    # считаются границы суток: «Сегодня», серии, дайджест. Определяется
    # браузером автоматически и обновляется при переезде; NULL означает, что
    # браузер ещё не успел сообщить — тогда берётся пояс по умолчанию.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # True — пояс приходит от браузера и сам меняется при переезде. False —
    # человек выбрал пояс руками в настройках, и браузер его больше не трогает:
    # у того, кто живёт на два города, своё мнение о том, где у него день.
    timezone_auto: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Поколение сессий. Сессия живёт только в подписанной cookie, серверного
    # хранилища нет — то есть отозвать её нечем: смена пароля не выкидывала
    # того, кто увёл cookie, и она работала все две недели. Номер поколения
    # кладётся в cookie при входе; смена пароля увеличивает его, и все старые
    # cookie перестают подходить.
    session_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Согласие с условиями ИИ-помощника: 18+, ответы генерирует нейросеть,
    # запросы уходят провайдеру, чей ключ подключён. Показывается один раз.
    ai_terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
