"""Trips: payload per trip.

The payload used to live only on the vehicle. But it is a property of the
trip: the same route driven once with two people and once fully loaded gives
two different energy balances, especially on hills.

NULL means "the vehicle profile applied" - trips from before this field
thus do not retroactively claim a payload of zero kilograms.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.add_column(sa.Column('zuladung_kg', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.drop_column('zuladung_kg')
