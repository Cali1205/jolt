"""Fahrten: Zuladung je Fahrt.

Die Zuladung stand bisher nur am Fahrzeug. Sie ist aber eine Eigenschaft der
Fahrt: dieselbe Strecke einmal zu zweit und einmal voll beladen sind zwei
verschiedene Energiebilanzen, besonders am Berg.

NULL heisst "es galt das Fahrzeugprofil" - Fahrten aus der Zeit vor diesem
Feld behaupten so nicht rückwirkend eine Zuladung von null Kilogramm.

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
