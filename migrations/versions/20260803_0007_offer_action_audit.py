"""Persist requested and executed offer actions separately."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0007"
down_revision: str | None = "20260802_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("marketplace_offers", sa.Column("requested_action", sa.String(length=30)))
    op.add_column("marketplace_offers", sa.Column("executed_action", sa.String(length=30)))


def downgrade() -> None:
    op.drop_column("marketplace_offers", "executed_action")
    op.drop_column("marketplace_offers", "requested_action")
