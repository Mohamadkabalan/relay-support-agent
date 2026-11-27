"""Scenario drafts (Phase 6): eval scenarios drafted from conversations by the console.

`conversation_id` and `created_by` are plain UUIDs, not foreign keys: a draft outlives the demo
reset that deletes its conversation and operator (like `audit_log`, ADR-0005).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-26 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scenario_drafts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("yaml", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("problems", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('generating', 'valid', 'invalid', 'failed')",
            name=op.f("ck_scenario_drafts_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scenario_drafts")),
    )
    op.create_index(
        op.f("ix_scenario_drafts_conversation_id"), "scenario_drafts", ["conversation_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_scenario_drafts_conversation_id"), table_name="scenario_drafts")
    op.drop_table("scenario_drafts")
