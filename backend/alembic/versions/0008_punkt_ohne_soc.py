"""Live points: state of charge may be missing.

Previously every measurement point had to carry a state of charge. That
enforced exactly the behaviour that makes tracking unusable: the PWA only
reported something when someone typed in the state of charge - i.e. at
charging stops, and in between nothing at all for two hours. Without points
there is no position and no time either, and so the time factor stays at 1.0
for the whole trip and the arrival forecast stays at the departure state.

The car does not know its state of charge by itself, but it does know its
position - the phone delivers it every second, for free. The two quantities
have different sampling rates, and the schema has to allow for that:
position continuously, state of charge occasionally.

What applies between two reported states of charge is extrapolated by
`live/sitzung.py` from the energy profile - which knows the slope, speed and
weather of the route. That is exactly what it is for.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.alter_column('soc', existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    # Points without a state of charge must go before the column becomes NOT NULL again -
    # otherwise the downgrade fails on exactly the data it produced.
    op.execute('DELETE FROM live_punkte WHERE soc IS NULL')
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.alter_column('soc', existing_type=sa.Float(), nullable=False)
