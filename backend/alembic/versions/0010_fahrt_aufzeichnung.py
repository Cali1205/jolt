"""Trips: recorded instead of planned.

Until now a trip always originated from planning - start, destination, route
from the routing service, profile calculated. The reverse path was missing:
drive off, record, and let the route emerge afterwards from what was
recorded.

It is needed for calibration. A known short route, always the same, driven a
few times, is the cleanest consumption measurement there is - and having to
plan a route first is cumbersome enough that you let it be.

The flag distinguishes the two: for a recording, geometry and energy profile
are empty at the start and are built when it ends (see
`live/aufzeichnung.py`).

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.add_column(sa.Column('aufzeichnung', sa.Boolean(),
                                      nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.drop_column('aufzeichnung')
