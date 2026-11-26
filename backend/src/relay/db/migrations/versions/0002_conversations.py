"""Conversations: the agent runtime's transcript, turns, model and tool calls, inbox and events.

`messages` is append-only like `audit_log`: a statement-level trigger rejects UPDATE and DELETE
(TRUNCATE, which the seed reset uses, is not affected). Transcript payloads are text checked with
`IS JSON` rather than `jsonb`, so replays send back the exact bytes (ADR-0005).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26 11:54:38.958722
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MESSAGES_APPEND_ONLY_FUNCTION = """
CREATE FUNCTION messages_reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'messages is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("effort", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("tool_manifest", sa.Text(), nullable=False),
        sa.Column("manifest_hash", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("cost_complete", sa.Boolean(), nullable=False),
        sa.Column("injection_flags", sa.Integer(), nullable=False),
        sa.Column("budget_warned", sa.Boolean(), nullable=False),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'escalated', 'closed')",
            name=op.f("ck_conversations_status_valid"),
        ),
        sa.CheckConstraint(
            "cost_micros >= 0", name=op.f("ck_conversations_cost_micros_non_negative")
        ),
        sa.CheckConstraint(
            "tool_manifest IS JSON", name=op.f("ck_conversations_tool_manifest_is_json")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_conversations_customer_id_customers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        op.f("ix_conversations_customer_id"), "conversations", ["customer_id"], unique=False
    )
    op.create_table(
        "turns",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("retracted", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)",
            name=op.f("ck_turns_finished_when_final"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'step_limit', 'budget_exhausted', 'refused', 'failed')",
            name=op.f("ck_turns_status_valid"),
        ),
        sa.CheckConstraint(
            "trigger IN ('customer_message', 'approval_resolved')",
            name=op.f("ck_turns_trigger_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_turns_conversation_id_conversations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turns")),
        sa.UniqueConstraint("conversation_id", "seq", name=op.f("uq_turns_conversation_id_seq")),
    )
    op.create_table(
        "conversation_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_events_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["turns.id"], name=op.f("fk_conversation_events_turn_id_turns")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_events")),
    )
    op.create_index(
        "ix_conversation_events_conversation_id_id",
        "conversation_events",
        ["conversation_id", "id"],
        unique=False,
    )
    op.create_table(
        "conversation_inbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('customer_message', 'approval_resolved')",
            name=op.f("ck_conversation_inbox_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_inbox_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["turns.id"], name=op.f("fk_conversation_inbox_turn_id_turns")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_inbox")),
    )
    op.create_index(
        "ix_conversation_inbox_unclaimed",
        "conversation_inbox",
        ["conversation_id", "id"],
        unique=False,
        postgresql_where=sa.text("turn_id IS NULL"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(kind = 'notice') = (raw IS NULL)", name=op.f("ck_messages_raw_unless_notice")
        ),
        sa.CheckConstraint(
            "kind IN ('customer', 'assistant', 'tool_results', 'system_note', 'event', 'notice')",
            name=op.f("ck_messages_kind_valid"),
        ),
        sa.CheckConstraint("raw IS NULL OR raw IS JSON", name=op.f("ck_messages_raw_is_json")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], name=op.f("fk_messages_turn_id_turns")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("conversation_id", "seq", name=op.f("uq_messages_conversation_id_seq")),
    )
    op.create_index(op.f("ix_messages_turn_id"), "messages", ["turn_id"], unique=False)
    op.execute(MESSAGES_APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER messages_append_only BEFORE UPDATE OR DELETE ON messages "
        "FOR EACH STATEMENT EXECUTE FUNCTION messages_reject_change()"
    )
    op.create_table(
        "model_calls",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("requested_model", sa.Text(), nullable=False),
        sa.Column("served_model", sa.Text(), nullable=False),
        sa.Column("effort", sa.Text(), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=False),
        sa.Column("refusal_category", sa.Text(), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("input_transformations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("thinking", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["turns.id"], name=op.f("fk_model_calls_turn_id_turns")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_calls")),
        sa.UniqueConstraint("turn_id", "seq", name=op.f("uq_model_calls_turn_id_seq")),
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("tool_use_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("is_error", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('ok', 'error', 'duplicate', 'rejected', 'unavailable')",
            name=op.f("ck_tool_calls_outcome_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["turns.id"], name=op.f("fk_tool_calls_turn_id_turns")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
        sa.UniqueConstraint("turn_id", "seq", name=op.f("uq_tool_calls_turn_id_seq")),
    )


def downgrade() -> None:
    op.drop_table("tool_calls")
    op.drop_table("model_calls")
    op.execute("DROP TRIGGER IF EXISTS messages_append_only ON messages")
    op.execute("DROP FUNCTION IF EXISTS messages_reject_change()")
    op.drop_index(op.f("ix_messages_turn_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(
        "ix_conversation_inbox_unclaimed",
        table_name="conversation_inbox",
        postgresql_where=sa.text("turn_id IS NULL"),
    )
    op.drop_table("conversation_inbox")
    op.drop_index("ix_conversation_events_conversation_id_id", table_name="conversation_events")
    op.drop_table("conversation_events")
    op.drop_table("turns")
    op.drop_index(op.f("ix_conversations_customer_id"), table_name="conversations")
    op.drop_table("conversations")
