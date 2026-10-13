"""History of the battery capacity the vehicle reports.

`vehicles.measured_capacity_kwh` holds only the latest value. For the state
of health the curve over the odometer is needed, so every change (and at
least one value per week) is kept in a table of its own. The values already
stored on the points are not back-filled: the points hold them per reading
and the evaluation can still read them from there.

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capacity_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vehicle_id", sa.Integer(),
                  sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("measured_at", sa.DateTime(), nullable=False),
        sa.Column("capacity_kwh", sa.Float(), nullable=False),
        sa.Column("odometer_km", sa.Float(), nullable=True),
    )
    op.create_index("ix_capacity_history_vehicle_id", "capacity_history",
                    ["vehicle_id"])


def downgrade() -> None:
    op.drop_index("ix_capacity_history_vehicle_id", table_name="capacity_history")
    op.drop_table("capacity_history")
