"""Live points: store the raw values of the source.

`LivePunkt` holds exactly what the tracking calculates with: position,
state of charge, speed, outside temperature. An OBD2 logger delivers more,
though - pack voltage, current, odometer, interior temperature - and above
all it delivers the **unprocessed** value from which the state of charge
was derived.

This is not hoarding, it solves a concrete problem. The ID.Buzz state of
charge arrives as a single byte from which two numbers result: the gross
value of the battery and the one the display shows. The conversion between
the two is documented, but for the ID.3 - for the ID.Buzz it is off by a
good percentage point, and not by a constant amount. To correct it, you need
measurement points with the raw value **and** what the display showed in the
same minute. Whoever does not record this has to drive out a second time for
it.

Deliberately JSON and not one column per measured quantity: which values a
source delivers is not fixed and will change. One column per quantity would
mean one migration per data identifier that someone finds interesting.
These values are not used for calculation - that is what the typed fields
are for; this is where what you want to be able to analyse later is kept.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rohwerte', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.drop_column('rohwerte')
