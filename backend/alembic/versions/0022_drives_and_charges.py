"""Logbook: drives and charges found in the VW data and in jolt's recordings.

Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vehicle_drives",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vehicle_id", sa.Integer(),
                  sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=False),
        sa.Column("odo_start", sa.Float(), nullable=True),
        sa.Column("odo_end", sa.Float(), nullable=True),
        sa.Column("distance_km", sa.Float(), nullable=False),
        sa.Column("soc_start", sa.Float(), nullable=True),
        sa.Column("soc_end", sa.Float(), nullable=True),
        sa.Column("energy_used_kwh", sa.Float(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("live_session_id", sa.Integer(),
                  sa.ForeignKey("live_sessions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_vehicle_drives_vehicle_id", "vehicle_drives",
                    ["vehicle_id"])
    op.create_index("ix_vehicle_drives_started_at", "vehicle_drives",
                    ["started_at"])
    op.create_table(
        "vehicle_charges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vehicle_id", sa.Integer(),
                  sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=False),
        sa.Column("soc_start", sa.Float(), nullable=True),
        sa.Column("soc_end", sa.Float(), nullable=True),
        sa.Column("energy_kwh", sa.Float(), nullable=False),
        sa.Column("max_power_kw", sa.Float(), nullable=False),
        sa.Column("avg_power_kw", sa.Float(), nullable=True),
        sa.Column("charge_type", sa.String(length=2), nullable=True),
        sa.Column("odometer_km", sa.Float(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_vehicle_charges_vehicle_id", "vehicle_charges",
                    ["vehicle_id"])
    op.create_index("ix_vehicle_charges_started_at", "vehicle_charges",
                    ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_vehicle_charges_started_at", table_name="vehicle_charges")
    op.drop_index("ix_vehicle_charges_vehicle_id", table_name="vehicle_charges")
    op.drop_table("vehicle_charges")
    op.drop_index("ix_vehicle_drives_started_at", table_name="vehicle_drives")
    op.drop_index("ix_vehicle_drives_vehicle_id", table_name="vehicle_drives")
    op.drop_table("vehicle_drives")
