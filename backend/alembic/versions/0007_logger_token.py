"""Vehicles: long-lived token for a logger in the car.

Measurement points used to come in exclusively via
`/api/live/{sitzung_id}/punkt`. That suits the PWA, which started the trip
itself and therefore knows the ID - but not a device installed in the car
that simply starts sending when the car is switched on. The session ID
changes with every trip; a dongle cannot know it.

With this token, the *vehicle* identifies itself instead, and the backend
looks up its running session. NULL means "no logger set up".

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fahrzeuge', schema=None) as batch_op:
        batch_op.add_column(sa.Column('logger_token', sa.String(64),
                                      nullable=True))
        # Unique, so that a token never matches two vehicles. Several NULLs
        # are unaffected - vehicles without a logger do not interfere with each other.
        batch_op.create_index('ix_fahrzeuge_logger_token', ['logger_token'],
                              unique=True)


def downgrade() -> None:
    with op.batch_alter_table('fahrzeuge', schema=None) as batch_op:
        batch_op.drop_index('ix_fahrzeuge_logger_token')
        batch_op.drop_column('logger_token')
