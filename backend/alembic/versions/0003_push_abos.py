"""Web Push: Abos der Geraete, die Benachrichtigungen bekommen wollen.

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
        # Die Adresse beim Push-Dienst. Eindeutig, weil derselbe Browser sie
        # erneut liefert - ein zweites Abo hiesse doppelte Benachrichtigungen.
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
