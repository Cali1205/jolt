"""Web Push: subscriptions of the devices that want to receive notifications.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'push_abos',
        sa.Column('id', sa.Integer(), nullable=False),
        # The address at the push service. Unique, because the same browser
        # delivers it again - a second subscription would mean duplicate notifications.
        sa.Column('endpoint', sa.String(length=500), nullable=False),
        sa.Column('p256dh', sa.String(length=200), nullable=False),
        sa.Column('auth', sa.String(length=100), nullable=False),
        sa.Column('geraet', sa.String(length=120), nullable=True),
        sa.Column('fehler', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('angelegt', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint', name='uq_push_abo_endpoint'),
    )
    with op.batch_alter_table('push_abos', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_push_abos_endpoint'),
                              ['endpoint'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('push_abos', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_push_abos_endpoint'))
    op.drop_table('push_abos')
