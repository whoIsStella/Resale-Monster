"""Add eBay account state and rate-limit persistence.

Revision ID: 20260921_0009
Revises: 20260804_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0009"
down_revision: str | None = "20260804_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_state_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(length=1000), nullable=False),
        sa.Column("requested_scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["marketplace_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_state_records")),
        sa.UniqueConstraint("state_digest", name="oauth_state_digest"),
    )
    op.create_index(
        "ix_oauth_state_account",
        "oauth_state_records",
        ["account_id", "expires_at"],
        unique=False,
    )

    op.create_table(
        "marketplace_rate_limit_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("operation_family", sa.String(length=80), nullable=False),
        sa.Column("allowance", sa.Integer(), nullable=True),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_after_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("throttle_count", sa.Integer(), nullable=False),
        sa.Column("safety_margin", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["marketplace_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_marketplace_rate_limit_state")),
        sa.UniqueConstraint(
            "account_id", "operation_family", name="rate_limit_account_family"
        ),
    )
    op.create_index(
        "ix_marketplace_rate_limit_state_account_id",
        "marketplace_rate_limit_state",
        ["account_id"],
        unique=False,
    )

    with op.batch_alter_table("marketplace_accounts") as batch:
        batch.add_column(sa.Column("remote_account_identifier", sa.String(length=200)))
        batch.add_column(sa.Column("display_seller_name", sa.String(length=100)))
        batch.add_column(
            sa.Column(
                "granted_scopes",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(sa.Column("credential_version", sa.Integer()))
        batch.add_column(sa.Column("authentication_expires_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column("last_authentication_check_at", sa.DateTime(timezone=True))
        )
        batch.add_column(
            sa.Column("last_failed_authentication_check_at", sa.DateTime(timezone=True))
        )
        batch.add_column(sa.Column("degradation_reason", sa.Text()))
        batch.add_column(sa.Column("disabled_reason", sa.Text()))

    with op.batch_alter_table("marketplace_operation_attempts") as batch:
        batch.add_column(sa.Column("job_id", sa.Uuid()))
        batch.add_column(sa.Column("requested_payload_hash", sa.String(length=64)))
        batch.add_column(sa.Column("remote_request_id", sa.String(length=200)))
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column(
                "verification_status",
                sa.String(length=11),
                nullable=False,
                server_default="pending",
            )
        )
        batch.add_column(
            sa.Column(
                "first_attempted_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch.add_column(
            sa.Column(
                "last_attempted_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )

    with op.batch_alter_table("marketplace_operation_attempts") as batch:
        batch.create_check_constraint(
            "ck_marketplace_operation_attempts_marketplace_verification_status",
            "verification_status IN ('pending', 'verified', 'mismatched', 'unavailable', 'failed')",
        )


def downgrade() -> None:
    with op.batch_alter_table("marketplace_operation_attempts") as batch:
        batch.drop_constraint(
            "ck_marketplace_operation_attempts_marketplace_verification_status",
            type_="check",
        )

    with op.batch_alter_table("marketplace_operation_attempts") as batch:
        batch.drop_column("last_attempted_at")
        batch.drop_column("first_attempted_at")
        batch.drop_column("verification_status")
        batch.drop_column("attempt_count")
        batch.drop_column("remote_request_id")
        batch.drop_column("requested_payload_hash")
        batch.drop_column("job_id")

    with op.batch_alter_table("marketplace_accounts") as batch:
        batch.drop_column("disabled_reason")
        batch.drop_column("degradation_reason")
        batch.drop_column("last_failed_authentication_check_at")
        batch.drop_column("last_authentication_check_at")
        batch.drop_column("authentication_expires_at")
        batch.drop_column("credential_version")
        batch.drop_column("granted_scopes")
        batch.drop_column("display_seller_name")
        batch.drop_column("remote_account_identifier")

    op.drop_index(
        "ix_marketplace_rate_limit_state_account_id",
        table_name="marketplace_rate_limit_state",
    )
    op.drop_table("marketplace_rate_limit_state")
    op.drop_index("ix_oauth_state_account", table_name="oauth_state_records")
    op.drop_table("oauth_state_records")
