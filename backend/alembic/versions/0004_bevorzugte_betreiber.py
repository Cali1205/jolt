"""Fahrzeuge: bevorzugte Ladeanbieter fuer den Optimierer.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fahrzeuge', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bevorzugte_betreiber', sa.JSON(),
                                      nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('fahrzeuge', schema=None) as batch_op:
        batch_op.drop_column('bevorzugte_betreiber')
