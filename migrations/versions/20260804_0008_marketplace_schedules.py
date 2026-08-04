"""durable marketplace schedule metadata

Revision ID: 20260804_0008
Revises: 20260803_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0008"
down_revision: str | None = "20260803_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(
            sa.Column(
                "schedule_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
            )
        )
        batch.add_column(sa.Column("last_job_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("last_job_id")
        batch.drop_column("schedule_metadata")
