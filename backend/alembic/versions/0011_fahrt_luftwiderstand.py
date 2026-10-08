"""Trips: surcharge on the air drag.

A bike rack on the tow bar or a roof box belongs to the **trip**, not to the
vehicle - like the payload. Its mass could already be modelled via
`zuladung_kg`, but it is the smaller item: at motorway speed a rack costs
mainly air drag, and that goes with the square of the speed.

The real reason to model it is a different one, though: without it, its
consumption ends up in the **vehicle's correction factor** - and that
applies permanently and to all trips. A single holiday trip with racks would
distort everyday planning. The comment in `energie/kalibrierung.py` already
warns against exactly this ("a trip with an unnoticed roof box").

1.0 means "nothing attached". The value multiplies the drag coefficient;
what it really has to be is only known after a recorded trip with a rack.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.add_column(sa.Column('luftwiderstand_faktor', sa.Float(),
                                      nullable=False, server_default='1.0'))


def downgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.drop_column('luftwiderstand_faktor')
