"""Snapshots from a manufacturer cloud (the VW EU Data Act portal).

One row per report of the car, keyed by vehicle, source and the time the
car reported, so fetching the same report again stores nothing new.

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vehicle_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vehicle_id", sa.Integer(),
                  sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.UniqueConstraint("vehicle_id", "source", "captured_at",
                            name="uq_vehicle_snapshot"),
    )
    op.create_index("ix_vehicle_snapshots_vehicle_id", "vehicle_snapshots",
                    ["vehicle_id"])


def downgrade() -> None:
    op.drop_index("ix_vehicle_snapshots_vehicle_id",
                  table_name="vehicle_snapshots")
    op.drop_table("vehicle_snapshots")
