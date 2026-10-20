"""Logbook charges without a power reading.

A source that reports no charge power (Stellantis) still shows a charge
through its charge state and the rise of the charge level; its maximum
power is unknown, so the column may be NULL.

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicle_charges", schema=None) as batch_op:
        batch_op.alter_column("max_power_kw", existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE vehicle_charges SET max_power_kw = 0 WHERE max_power_kw IS NULL")
    with op.batch_alter_table("vehicle_charges", schema=None) as batch_op:
        batch_op.alter_column("max_power_kw", existing_type=sa.Float(), nullable=False)
