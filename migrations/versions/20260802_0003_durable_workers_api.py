"""Add durable workers, leases, schedules, authentication, and idempotency."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0003"
down_revision: str | None = "20260802_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _job_columns():
    return [
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_reason", sa.Text(), nullable=True),
        sa.Column("prior_worker_id", sa.String(200), nullable=True),
        sa.Column("prior_lease_identifier", sa.String(128), nullable=True),
        sa.Column("recovery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
    ]


def _add_job_columns() -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table("agent_job_records", recreate="always") as batch:
            for column in _job_columns():
                batch.add_column(column)
        with op.batch_alter_table("agent_job_records") as batch:
            batch.alter_column("recovery_count", server_default=None)
    else:
        for column in _job_columns():
            op.add_column("agent_job_records", column)
        op.alter_column("agent_job_records", "recovery_count", server_default=None)


def _drop_job_columns() -> None:
    names = [column.name for column in _job_columns()]
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table("agent_job_records", recreate="always") as batch:
            for name in reversed(names):
                batch.drop_column(name)
    else:
        for name in reversed(names):
            op.drop_column("agent_job_records", name)


def upgrade() -> None:
    _add_job_columns()
    op.create_table(
        "worker_records",
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("configured_concurrency", sa.Integer(), nullable=False),
        sa.Column("active_job_count", sa.Integer(), nullable=False),
        sa.Column("software_version", sa.String(50), nullable=False),
        sa.Column("shutdown_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_records")),
    )
    op.create_table(
        "job_leases",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(200), nullable=False),
        sa.Column("lease_identifier", sa.String(128), nullable=False),
        sa.Column("lease_token_hash", sa.String(128), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["agent_job_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_job_leases")),
        sa.UniqueConstraint("lease_identifier", name=op.f("uq_job_leases_lease_identifier")),
    )
    op.create_index(op.f("ix_job_leases_worker_id"), "job_leases", ["worker_id"])
    op.create_index(op.f("ix_job_leases_expires_at"), "job_leases", ["expires_at"])
    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(200), nullable=False),
        sa.Column("lease_identifier", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("failure_category", sa.String(100), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["agent_job_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_attempts")),
        sa.UniqueConstraint("job_id", "attempt_number", name=op.f("uq_execution_attempts_job_id")),
    )
    op.create_index(op.f("ix_execution_attempts_job_id"), "execution_attempts", ["job_id"])
    op.create_table(
        "schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("task_type", sa.String(100), nullable=False),
        sa.Column("objective_template", sa.Text(), nullable=False),
        sa.Column("requested_agent", sa.String(100), nullable=True),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("context_files", sa.JSON(), nullable=False),
        sa.Column("timing_type", sa.String(20), nullable=False),
        sa.Column("timing_value", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("missed_run_policy", sa.String(30), nullable=False),
        sa.Column("max_catch_up_runs", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedules")),
        sa.UniqueConstraint("name", name=op.f("uq_schedules_name")),
    )
    op.create_index(op.f("ix_schedules_next_run_at"), "schedules", ["next_run_at"])
    op.create_table(
        "api_principals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_principals")),
        sa.UniqueConstraint("name", name=op.f("uq_api_principals_name")),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["principal_id"], ["api_principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.create_index(op.f("ix_api_keys_principal_id"), "api_keys", ["principal_id"])
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["api_principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_records")),
        sa.UniqueConstraint(
            "principal_id", "idempotency_key", name=op.f("uq_idempotency_records_principal_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_index(op.f("ix_api_keys_principal_id"), table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("api_principals")
    op.drop_index(op.f("ix_schedules_next_run_at"), table_name="schedules")
    op.drop_table("schedules")
    op.drop_index(op.f("ix_execution_attempts_job_id"), table_name="execution_attempts")
    op.drop_table("execution_attempts")
    op.drop_index(op.f("ix_job_leases_expires_at"), table_name="job_leases")
    op.drop_index(op.f("ix_job_leases_worker_id"), table_name="job_leases")
    op.drop_table("job_leases")
    op.drop_table("worker_records")
    _drop_job_columns()
