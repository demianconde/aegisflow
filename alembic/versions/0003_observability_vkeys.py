"""observabilidade + chaves virtuais

usage_logs: status, api_key_id, prompt_preview, response_preview
nexus_api_keys: monthly_budget_usd, rpm_limit, allowed_models

Revision ID: 0003_observability_vkeys
Revises: 0002_provider_key_fields
Create Date: 2026-07-14

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_observability_vkeys"
down_revision: str | None = "0002_provider_key_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # usage_logs — observabilidade
    op.add_column("usage_logs", sa.Column("api_key_id", sa.Uuid(), nullable=True))
    op.add_column("usage_logs", sa.Column("status", sa.String(20), nullable=False, server_default="ok"))
    op.add_column("usage_logs", sa.Column("prompt_preview", sa.String(500), nullable=True))
    op.add_column("usage_logs", sa.Column("response_preview", sa.String(500), nullable=True))
    op.create_index("ix_usage_logs_api_key_id", "usage_logs", ["api_key_id"])

    # nexus_api_keys — chaves virtuais
    op.add_column("nexus_api_keys", sa.Column("monthly_budget_usd", sa.Numeric(12, 4), nullable=True))
    op.add_column("nexus_api_keys", sa.Column("rpm_limit", sa.Integer(), nullable=True))
    op.add_column("nexus_api_keys", sa.Column("allowed_models", sa.String(1000), nullable=True))


def downgrade() -> None:
    op.drop_column("nexus_api_keys", "allowed_models")
    op.drop_column("nexus_api_keys", "rpm_limit")
    op.drop_column("nexus_api_keys", "monthly_budget_usd")
    op.drop_index("ix_usage_logs_api_key_id", table_name="usage_logs")
    op.drop_column("usage_logs", "response_preview")
    op.drop_column("usage_logs", "prompt_preview")
    op.drop_column("usage_logs", "status")
    op.drop_column("usage_logs", "api_key_id")
