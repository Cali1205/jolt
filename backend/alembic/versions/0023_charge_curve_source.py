"""Vehicles: plan with the measured charging curve or the profile's.

Existing vehicles get "auto": the measured curve once the logbook has enough
DC charges, the profile curve until then.

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("charge_curve_source", sa.String(length=10),
                                      nullable=False, server_default="auto"))


def downgrade() -> None:
    with op.batch_alter_table("vehicles", schema=None) as batch_op:
        batch_op.drop_column("charge_curve_source")
