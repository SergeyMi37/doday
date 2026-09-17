"""users.timezone — часовой пояс пользователя

Границы суток («Сегодня», «Ближайшие», серии, дайджест) считались в UTC.
Для московского пользователя это значило, что день заканчивается в три часа
ночи, а задача на 01:00 понедельника показывалась в воскресном списке.

Храним имя IANA («Europe/Moscow»), а не смещение: смещение меняется дважды в
год в половине стран мира, имя — нет.

NULL допустим: это «браузер ещё не сообщил», тогда берётся пояс по умолчанию.

Revision ID: 0060
Revises: 0059
"""

import sqlalchemy as sa

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("timezone", sa.String(length=64), nullable=True))
    op.add_column(
        "users",
        sa.Column("timezone_auto", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "telegram_links",
        sa.Column("last_digest_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telegram_links", "last_digest_sent_at")
    op.drop_column("users", "timezone_auto")
    op.drop_column("users", "timezone")
