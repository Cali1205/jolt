"""Logbook drives with an estimated start.

A sparse source that said nothing between the car's last parked report and
the drive knows only the gap the drive began in; the start is then reckoned
back from the distance, and the row says so.

Revision ID: 0027
Revises: 0026
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicle_drives", schema=None) as batch_op:
        batch_op.add_column(sa.Column("start_estimated", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("vehicle_drives", schema=None) as batch_op:
        batch_op.drop_column("start_estimated")
