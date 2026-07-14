"""guardrails por tenant (pii + termos bloqueados)

Revision ID: 0006_guardrails
Revises: 0005_sub_period_lead_unique
Create Date: 2026-07-14

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_guardrails"
down_revision: str | None = "0005_sub_period_lead_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("guardrail_pii", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("tenants", sa.Column("guardrail_blocked_terms", sa.String(2000), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "guardrail_blocked_terms")
    op.drop_column("tenants", "guardrail_pii")
