"""The API (Phase 5): a `paused` turn status and the order numbers of demo persona clones.

- A turn is `paused` when the daily spend cap stopped it before a model call (PLAN.md §12).
- Each demo visitor gets private copies of the personas (D4). Their orders are numbered from
  `demo_order_number_seq`, PV-801000 to PV-899999, which no seeded or eval order uses.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-26 21:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'running', 'completed', 'step_limit', 'budget_exhausted', 'refused', 'failed'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_turns_status_valid"), "turns", type_="check")
    op.create_check_constraint(
        op.f("ck_turns_status_valid"), "turns", f"status IN ({STATUSES}, 'paused')"
    )
    op.execute("CREATE SEQUENCE demo_order_number_seq START WITH 801000 MAXVALUE 899999")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS demo_order_number_seq")
    # Fails while paused turns exist: delete them first if you really mean to go back.
    op.drop_constraint(op.f("ck_turns_status_valid"), "turns", type_="check")
    op.create_check_constraint(op.f("ck_turns_status_valid"), "turns", f"status IN ({STATUSES})")
