"""Baseline schema: the store, previews, approvals, escalations, the audit log and the knowledge base.

Enumerations are text columns with CHECK constraints (relay.db.base.TextEnum). The conversation
tables, and the foreign keys from `conversation_id` columns to them, arrive in Phase 3.

Revision ID: 0001
Revises:
Create Date: 2026-11-25 08:25:52.671030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEQUENCES = {
    "rma_number_seq": 100001,
    "refund_number_seq": 10001,
    "approval_number_seq": 10001,
    "escalation_number_seq": 10001,
}

AUDIT_APPEND_ONLY_FUNCTION = """
CREATE FUNCTION audit_log_reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for name, start in SEQUENCES.items():
        op.execute(f"CREATE SEQUENCE {name} START WITH {start}")

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("on_behalf_of", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "actor_type IN ('agent', 'operator', 'system')",
            name=op.f("ck_audit_log_actor_type_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity", "entity_id"], unique=False)
    op.create_index(op.f("ix_audit_log_on_behalf_of"), "audit_log", ["on_behalf_of"], unique=False)
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("demo_visitor_id", sa.Text(), nullable=True),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_customers_email_lowercase")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("email", name=op.f("uq_customers_email")),
    )
    op.create_index(
        op.f("ix_customers_demo_visitor_id"), "customers", ["demo_visitor_id"], unique=False
    )
    op.create_table(
        "help_articles",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_help_articles")),
        sa.UniqueConstraint("slug", name=op.f("uq_help_articles_slug")),
    )
    op.create_table(
        "operators",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_operators_email_lowercase")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operators")),
        sa.UniqueConstraint("email", name=op.f("uq_operators_email")),
    )
    op.create_table(
        "products",
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("condition", sa.Text(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("final_sale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "condition IN ('new', 'refurbished')", name=op.f("ck_products_condition_valid")
        ),
        sa.CheckConstraint(
            "kind IN ('physical', 'gift_card', 'digital')", name=op.f("ck_products_kind_valid")
        ),
        sa.CheckConstraint("price_cents >= 0", name=op.f("ck_products_price_cents_non_negative")),
        sa.PrimaryKeyConstraint("sku", name=op.f("pk_products")),
    )
    op.create_table(
        "seed_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("seeded_now", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generator_version", sa.Text(), nullable=False),
        sa.Column("data_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seed_runs")),
    )
    op.create_table(
        "addresses",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_name", sa.Text(), nullable=False),
        sa.Column("line1", sa.Text(), nullable=False),
        sa.Column("line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("postal_code", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), server_default="US", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_addresses_customer_id_customers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_addresses")),
    )
    op.create_index(op.f("ix_addresses_customer_id"), "addresses", ["customer_id"], unique=False)
    op.create_index(
        "uq_addresses_one_default",
        "addresses",
        ["customer_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', heading), 'A') || setweight(to_tsvector('english', text), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.ForeignKeyConstraint(
            ["article_id"], ["help_articles.id"], name=op.f("fk_kb_chunks_article_id_help_articles")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunks")),
        sa.UniqueConstraint("article_id", "ordinal", name=op.f("uq_kb_chunks_article_id_ordinal")),
    )
    op.create_index(
        "ix_kb_chunks_embedding",
        "kb_chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_kb_chunks_tsv", "kb_chunks", ["tsv"], unique=False, postgresql_using="gin")
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("promised_delivery_on", sa.Date(), nullable=True),
        sa.Column("shipping_address", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payment_brand", sa.Text(), nullable=False),
        sa.Column("payment_last4", sa.Text(), nullable=False),
        sa.Column("shipping_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("customer_note", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("number ~ '^PV-[0-9]{6}$'", name=op.f("ck_orders_number_format")),
        sa.CheckConstraint(
            "payment_last4 ~ '^[0-9]{4}$'", name=op.f("ck_orders_payment_last4_format")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'shipped', 'delivered', 'cancelled')",
            name=op.f("ck_orders_status_valid"),
        ),
        sa.CheckConstraint(
            "shipping_cents >= 0", name=op.f("ck_orders_shipping_cents_non_negative")
        ),
        sa.CheckConstraint("total_cents >= 0", name=op.f("ck_orders_total_cents_non_negative")),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_orders_customer_id_customers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("number", name=op.f("uq_orders_number")),
    )
    op.create_index(
        "ix_orders_customer_placed", "orders", ["customer_id", "placed_at"], unique=False
    )
    op.create_table(
        "action_previews",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("issued_turn_seq", sa.Integer(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("canonical_args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("needs_approval", sa.Boolean(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "action IN ('change_address', 'create_return', 'issue_refund')",
            name=op.f("ck_action_previews_action_valid"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('completed', 'pending_approval', 'denied')",
            name=op.f("ck_action_previews_outcome_valid"),
        ),
        sa.CheckConstraint(
            "(committed_at IS NULL) = (outcome IS NULL)",
            name=op.f("ck_action_previews_committed_has_outcome"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_action_previews_expires_after_creation")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_action_previews_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_action_previews_order_id_orders")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_previews")),
        sa.UniqueConstraint("token", name=op.f("uq_action_previews_token")),
    )
    op.create_index(
        op.f("ix_action_previews_customer_id"), "action_previews", ["customer_id"], unique=False
    )
    op.create_table(
        "order_lines",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("line_no > 0", name=op.f("ck_order_lines_line_no_positive")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_order_lines_quantity_positive")),
        sa.CheckConstraint(
            "unit_price_cents >= 0", name=op.f("ck_order_lines_unit_price_cents_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_lines_order_id_orders")
        ),
        sa.ForeignKeyConstraint(
            ["sku"], ["products.sku"], name=op.f("fk_order_lines_sku_products")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_lines")),
        sa.UniqueConstraint("order_id", "line_no", name=op.f("uq_order_lines_order_id_line_no")),
    )
    op.create_table(
        "shipments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("carrier", sa.Text(), nullable=False),
        sa.Column("tracking_number", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eta", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "status IN ('label_created', 'in_transit', 'out_for_delivery', 'delivered', 'lost')",
            name=op.f("ck_shipments_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_shipments_order_id_orders")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipments")),
        sa.UniqueConstraint("order_id", name=op.f("uq_shipments_order_id")),
        sa.UniqueConstraint("tracking_number", name=op.f("uq_shipments_tracking_number")),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("approval_number", sa.Text(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("rules", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("execution_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)",
            name=op.f("ck_approvals_decided_when_final"),
        ),
        sa.CheckConstraint(
            "action IN ('change_address', 'create_return', 'issue_refund')",
            name=op.f("ck_approvals_action_valid"),
        ),
        sa.CheckConstraint(
            "approval_number ~ '^APR-[0-9]{5,}$'", name=op.f("ck_approvals_approval_number_format")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'executed', 'failed', 'rejected', 'expired')",
            name=op.f("ck_approvals_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_approvals_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["operators.id"], name=op.f("fk_approvals_decided_by_operators")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_approvals_order_id_orders")
        ),
        sa.ForeignKeyConstraint(
            ["preview_id"],
            ["action_previews.id"],
            name=op.f("fk_approvals_preview_id_action_previews"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.UniqueConstraint("approval_number", name=op.f("uq_approvals_approval_number")),
        sa.UniqueConstraint("preview_id", name=op.f("uq_approvals_preview_id")),
    )
    op.create_index(op.f("ix_approvals_customer_id"), "approvals", ["customer_id"], unique=False)
    op.create_index(
        "ix_approvals_pending",
        "approvals",
        ["requested_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "returns",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("rma_number", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("label_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_type", sa.Text(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "created_by_type IN ('agent', 'operator', 'system')",
            name=op.f("ck_returns_created_by_type_valid"),
        ),
        sa.CheckConstraint(
            "reason IN ('changed_mind', 'no_longer_needed', 'damaged_or_defective', 'wrong_item', 'not_as_described', 'other')",
            name=op.f("ck_returns_reason_valid"),
        ),
        sa.CheckConstraint(
            "rma_number ~ '^RMA-[0-9]{6}$'", name=op.f("ck_returns_rma_number_format")
        ),
        sa.CheckConstraint(
            "status IN ('label_issued', 'in_transit', 'received', 'completed', 'cancelled')",
            name=op.f("ck_returns_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_returns_order_id_orders")
        ),
        sa.ForeignKeyConstraint(
            ["preview_id"],
            ["action_previews.id"],
            name=op.f("fk_returns_preview_id_action_previews"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_returns")),
        sa.UniqueConstraint("preview_id", name=op.f("uq_returns_preview_id")),
        sa.UniqueConstraint("rma_number", name=op.f("uq_returns_rma_number")),
    )
    op.create_index(op.f("ix_returns_order_id"), "returns", ["order_id"], unique=False)
    op.create_table(
        "shipment_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["shipment_id"], ["shipments.id"], name=op.f("fk_shipment_events_shipment_id_shipments")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipment_events")),
    )
    op.create_index(
        "ix_shipment_events_shipment_time",
        "shipment_events",
        ["shipment_id", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "escalations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("escalation_number", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "escalation_number ~ '^ESC-[0-9]{5,}$'",
            name=op.f("ck_escalations_escalation_number_format"),
        ),
        sa.CheckConstraint(
            "priority IN ('normal', 'high')", name=op.f("ck_escalations_priority_valid")
        ),
        sa.CheckConstraint(
            "reason IN ('customer_requested', 'policy_dispute', 'suspected_fraud', 'complex_issue', 'upset_customer', 'technical_problem', 'approval_expired', 'other')",
            name=op.f("ck_escalations_reason_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')", name=op.f("ck_escalations_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"], ["approvals.id"], name=op.f("fk_escalations_approval_id_approvals")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_escalations_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"], ["operators.id"], name=op.f("fk_escalations_resolved_by_operators")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_escalations")),
        sa.UniqueConstraint("escalation_number", name=op.f("uq_escalations_escalation_number")),
    )
    op.create_index(
        op.f("ix_escalations_customer_id"), "escalations", ["customer_id"], unique=False
    )
    op.create_index(
        "uq_escalations_one_open_per_conversation",
        "escalations",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "refunds",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("refund_number", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("return_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "channel IN ('agent', 'staff', 'warehouse')", name=op.f("ck_refunds_channel_valid")
        ),
        sa.CheckConstraint(
            "reason IN ('damaged_or_defective', 'lost_in_transit', 'late_delivery', 'return_received')",
            name=op.f("ck_refunds_reason_valid"),
        ),
        sa.CheckConstraint(
            "refund_number ~ '^RF-[0-9]{5,}$'", name=op.f("ck_refunds_refund_number_format")
        ),
        sa.CheckConstraint("status IN ('issued')", name=op.f("ck_refunds_status_valid")),
        sa.CheckConstraint("amount_cents > 0", name=op.f("ck_refunds_amount_cents_positive")),
        sa.ForeignKeyConstraint(
            ["approval_id"], ["approvals.id"], name=op.f("fk_refunds_approval_id_approvals")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_refunds_order_id_orders")
        ),
        sa.ForeignKeyConstraint(
            ["preview_id"],
            ["action_previews.id"],
            name=op.f("fk_refunds_preview_id_action_previews"),
        ),
        sa.ForeignKeyConstraint(
            ["return_id"], ["returns.id"], name=op.f("fk_refunds_return_id_returns")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refunds")),
        sa.UniqueConstraint("preview_id", name=op.f("uq_refunds_preview_id")),
        sa.UniqueConstraint("refund_number", name=op.f("uq_refunds_refund_number")),
    )
    op.create_index(op.f("ix_refunds_order_id"), "refunds", ["order_id"], unique=False)
    op.create_table(
        "return_lines",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("return_id", sa.Uuid(), nullable=False),
        sa.Column("order_line_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_return_lines_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["order_line_id"],
            ["order_lines.id"],
            name=op.f("fk_return_lines_order_line_id_order_lines"),
        ),
        sa.ForeignKeyConstraint(
            ["return_id"], ["returns.id"], name=op.f("fk_return_lines_return_id_returns")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_return_lines")),
        sa.UniqueConstraint(
            "return_id", "order_line_id", name=op.f("uq_return_lines_return_id_order_line_id")
        ),
    )
    op.create_index(
        op.f("ix_return_lines_order_line_id"), "return_lines", ["order_line_id"], unique=False
    )
    op.create_table(
        "refund_lines",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("refund_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("order_line_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(kind = 'item') = (order_line_id IS NOT NULL AND quantity IS NOT NULL)",
            name=op.f("ck_refund_lines_item_lines_have_quantity"),
        ),
        sa.CheckConstraint("kind IN ('item', 'shipping')", name=op.f("ck_refund_lines_kind_valid")),
        sa.CheckConstraint("amount_cents > 0", name=op.f("ck_refund_lines_amount_cents_positive")),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0", name=op.f("ck_refund_lines_quantity_positive")
        ),
        sa.ForeignKeyConstraint(
            ["order_line_id"],
            ["order_lines.id"],
            name=op.f("fk_refund_lines_order_line_id_order_lines"),
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"], ["refunds.id"], name=op.f("fk_refund_lines_refund_id_refunds")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refund_lines")),
    )
    op.create_index(
        op.f("ix_refund_lines_order_line_id"), "refund_lines", ["order_line_id"], unique=False
    )

    # The audit log is append-only. A statement-level trigger rejects every UPDATE and DELETE,
    # even one that matches no rows. TRUNCATE stays possible for the table owner, which is what
    # `relay seed --reset` and the nightly demo reset run as.
    op.execute(AUDIT_APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_reject_change()")
    op.drop_index(op.f("ix_refund_lines_order_line_id"), table_name="refund_lines")
    op.drop_table("refund_lines")
    op.drop_index(op.f("ix_return_lines_order_line_id"), table_name="return_lines")
    op.drop_table("return_lines")
    op.drop_index(op.f("ix_refunds_order_id"), table_name="refunds")
    op.drop_table("refunds")
    op.drop_index(
        "uq_escalations_one_open_per_conversation",
        table_name="escalations",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_index(op.f("ix_escalations_customer_id"), table_name="escalations")
    op.drop_table("escalations")
    op.drop_index("ix_shipment_events_shipment_time", table_name="shipment_events")
    op.drop_table("shipment_events")
    op.drop_index(op.f("ix_returns_order_id"), table_name="returns")
    op.drop_table("returns")
    op.drop_index(
        "ix_approvals_pending",
        table_name="approvals",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(op.f("ix_approvals_customer_id"), table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("shipments")
    op.drop_table("order_lines")
    op.drop_index(op.f("ix_action_previews_customer_id"), table_name="action_previews")
    op.drop_table("action_previews")
    op.drop_index("ix_orders_customer_placed", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_kb_chunks_tsv", table_name="kb_chunks", postgresql_using="gin")
    op.drop_index(
        "ix_kb_chunks_embedding",
        table_name="kb_chunks",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("kb_chunks")
    op.drop_index(
        "uq_addresses_one_default", table_name="addresses", postgresql_where=sa.text("is_default")
    )
    op.drop_index(op.f("ix_addresses_customer_id"), table_name="addresses")
    op.drop_table("addresses")
    op.drop_table("seed_runs")
    op.drop_table("products")
    op.drop_table("operators")
    op.drop_table("help_articles")
    op.drop_index(op.f("ix_customers_demo_visitor_id"), table_name="customers")
    op.drop_table("customers")
    op.drop_index(op.f("ix_audit_log_on_behalf_of"), table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_table("audit_log")
    for name in SEQUENCES:
        op.execute(f"DROP SEQUENCE IF EXISTS {name}")
    # The vector extension stays: other databases on the server may use it, and it holds no data.
