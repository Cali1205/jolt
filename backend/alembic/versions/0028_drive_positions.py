"""Where a logbook drive began and ended.

From the car's own trip list (Stellantis) or the first and last GPS point
of a jolt recording; NULL where no source knows.

Revision ID: 0028
Revises: 0027
"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

COLUMNS = ("start_lat", "start_lon", "end_lat", "end_lon")


def upgrade() -> None:
    with op.batch_alter_table("vehicle_drives", schema=None) as batch_op:
        for name in COLUMNS:
            batch_op.add_column(sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vehicle_drives", schema=None) as batch_op:
        for name in reversed(COLUMNS):
            batch_op.drop_column(name)
