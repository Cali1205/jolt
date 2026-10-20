"""Keep every data point of a portal report, not only the interpreted ones.

Revision ID: 0021
Revises: 0020
"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicle_snapshots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("raw", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vehicle_snapshots", schema=None) as batch_op:
        batch_op.drop_column("raw")
