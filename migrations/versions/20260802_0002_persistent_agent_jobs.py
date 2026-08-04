"""Add persistent agent orchestration fields and lifecycle.

Revision ID: 20260802_0002
Revises: 20260802_0001
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0002"
down_revision: str | None = "20260802_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


old_job_status = sa.Enum(
    "queued",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    name="agent_job_status",
    native_enum=False,
    create_constraint=True,
)
new_job_status = sa.Enum(
    "pending",
    "queued",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    name="agent_job_status",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    if op.get_context().dialect.name == "sqlite":
        _upgrade_sqlite()
    else:
        _upgrade_standard()


def _upgrade_sqlite() -> None:
    with op.batch_alter_table("agent_job_records", recreate="always") as batch_op:
        batch_op.alter_column("agent_name", existing_type=sa.String(100), nullable=True)
        batch_op.alter_column(
            "status", existing_type=old_job_status, type_=new_job_status, existing_nullable=False
        )
        batch_op.add_column(sa.Column("requested_agent", sa.String(100), nullable=True))
        batch_op.add_column(
            sa.Column("workspace_path", sa.Text(), nullable=False, server_default=".")
        )
        batch_op.add_column(
            sa.Column("permissions", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("context_files", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("forbidden_actions", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="600")
        )
        batch_op.add_column(
            sa.Column("max_output_chars", sa.Integer(), nullable=False, server_default="100000")
        )
        batch_op.add_column(sa.Column("stdout", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("stderr", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("structured_result", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("failure_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("cancellation_reason", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("output_truncated", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("stdout_total_chars", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("stderr_total_chars", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("process_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint("timeout_positive", "timeout_seconds > 0")
        batch_op.create_check_constraint("max_output_chars_positive", "max_output_chars > 0")
        batch_op.create_check_constraint("version_positive", "version > 0")
        batch_op.create_index(
            "ix_agent_job_records_status_created", ["status", "created_at"], unique=False
        )

    for column in (
        "workspace_path",
        "permissions",
        "context_files",
        "forbidden_actions",
        "timeout_seconds",
        "max_output_chars",
        "stdout",
        "stderr",
        "output_truncated",
        "stdout_total_chars",
        "stderr_total_chars",
        "version",
    ):
        with op.batch_alter_table("agent_job_records") as batch_op:
            batch_op.alter_column(column, server_default=None)


def downgrade() -> None:
    if op.get_context().dialect.name == "sqlite":
        _downgrade_sqlite()
    else:
        _downgrade_standard()


def _downgrade_sqlite() -> None:
    jobs = sa.table(
        "agent_job_records",
        sa.column("status", sa.String()),
        sa.column("agent_name", sa.String()),
    )
    op.execute(
        sa.update(jobs).where(jobs.c.status.in_(["pending", "cancelled"])).values(status="failed")
    )
    op.execute(sa.update(jobs).where(jobs.c.agent_name.is_(None)).values(agent_name="unassigned"))
    with op.batch_alter_table("agent_job_records", recreate="always") as batch_op:
        batch_op.drop_index("ix_agent_job_records_status_created")
        batch_op.drop_constraint(op.f("ck_agent_job_records_version_positive"), type_="check")
        batch_op.drop_constraint(
            op.f("ck_agent_job_records_max_output_chars_positive"), type_="check"
        )
        batch_op.drop_constraint(op.f("ck_agent_job_records_timeout_positive"), type_="check")
        batch_op.drop_column("cancelled_at")
        batch_op.drop_column("queued_at")
        batch_op.drop_column("version")
        batch_op.drop_column("process_id")
        batch_op.drop_column("stderr_total_chars")
        batch_op.drop_column("stdout_total_chars")
        batch_op.drop_column("output_truncated")
        batch_op.drop_column("cancellation_reason")
        batch_op.drop_column("failure_reason")
        batch_op.drop_column("structured_result")
        batch_op.drop_column("stderr")
        batch_op.drop_column("stdout")
        batch_op.drop_column("max_output_chars")
        batch_op.drop_column("timeout_seconds")
        batch_op.drop_column("forbidden_actions")
        batch_op.drop_column("context_files")
        batch_op.drop_column("permissions")
        batch_op.drop_column("workspace_path")
        batch_op.drop_column("requested_agent")
        batch_op.alter_column(
            "status", existing_type=new_job_status, type_=old_job_status, existing_nullable=False
        )
        batch_op.alter_column("agent_name", existing_type=sa.String(100), nullable=False)


def _upgrade_standard() -> None:
    op.alter_column("agent_job_records", "agent_name", existing_type=sa.String(100), nullable=True)
    op.drop_constraint(
        op.f("ck_agent_job_records_agent_job_status"),
        "agent_job_records",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_agent_job_records_agent_job_status"),
        "agent_job_records",
        "status IN ('pending', 'queued', 'running', 'succeeded', 'failed', 'timed_out', "
        "'cancelled')",
    )
    columns = [
        sa.Column("requested_agent", sa.String(100), nullable=True),
        sa.Column("workspace_path", sa.Text(), nullable=False, server_default="."),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("context_files", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("forbidden_actions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="600"),
        sa.Column("max_output_chars", sa.Integer(), nullable=False, server_default="100000"),
        sa.Column("stdout", sa.Text(), nullable=False, server_default=""),
        sa.Column("stderr", sa.Text(), nullable=False, server_default=""),
        sa.Column("structured_result", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("output_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stdout_total_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stderr_total_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("process_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    ]
    for column in columns:
        op.add_column("agent_job_records", column)
    op.create_check_constraint(
        op.f("ck_agent_job_records_timeout_positive"),
        "agent_job_records",
        "timeout_seconds > 0",
    )
    op.create_check_constraint(
        op.f("ck_agent_job_records_max_output_chars_positive"),
        "agent_job_records",
        "max_output_chars > 0",
    )
    op.create_check_constraint(
        op.f("ck_agent_job_records_version_positive"),
        "agent_job_records",
        "version > 0",
    )
    op.create_index(
        "ix_agent_job_records_status_created",
        "agent_job_records",
        ["status", "created_at"],
        unique=False,
    )
    for column in (
        "workspace_path",
        "permissions",
        "context_files",
        "forbidden_actions",
        "timeout_seconds",
        "max_output_chars",
        "stdout",
        "stderr",
        "output_truncated",
        "stdout_total_chars",
        "stderr_total_chars",
        "version",
    ):
        op.alter_column("agent_job_records", column, server_default=None)


def _downgrade_standard() -> None:
    jobs = sa.table(
        "agent_job_records",
        sa.column("status", sa.String()),
        sa.column("agent_name", sa.String()),
    )
    op.execute(
        sa.update(jobs).where(jobs.c.status.in_(["pending", "cancelled"])).values(status="failed")
    )
    op.execute(sa.update(jobs).where(jobs.c.agent_name.is_(None)).values(agent_name="unassigned"))
    op.drop_index("ix_agent_job_records_status_created", table_name="agent_job_records")
    for name in (
        "version_positive",
        "max_output_chars_positive",
        "timeout_positive",
    ):
        op.drop_constraint(op.f(f"ck_agent_job_records_{name}"), "agent_job_records", type_="check")
    for column in (
        "cancelled_at",
        "queued_at",
        "version",
        "process_id",
        "stderr_total_chars",
        "stdout_total_chars",
        "output_truncated",
        "cancellation_reason",
        "failure_reason",
        "structured_result",
        "stderr",
        "stdout",
        "max_output_chars",
        "timeout_seconds",
        "forbidden_actions",
        "context_files",
        "permissions",
        "workspace_path",
        "requested_agent",
    ):
        op.drop_column("agent_job_records", column)
    op.drop_constraint(
        op.f("ck_agent_job_records_agent_job_status"),
        "agent_job_records",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_agent_job_records_agent_job_status"),
        "agent_job_records",
        "status IN ('queued', 'running', 'succeeded', 'failed', 'timed_out')",
    )
    op.alter_column("agent_job_records", "agent_name", existing_type=sa.String(100), nullable=False)
