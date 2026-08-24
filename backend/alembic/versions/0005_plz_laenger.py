"""Ladepunkte: plz-Spalte auf 40 Zeichen erweitern.

OCM liefert bei Standorten mit mehreren Postleitzahlen eine Semikolon-Liste
statt einer einzelnen PLZ - das sprengte die bisherigen 20 Zeichen und liess
den Streckenimport an einem realen Datensatz abbrechen.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('ladepunkte', schema=None) as batch_op:
        batch_op.alter_column('plz', existing_type=sa.String(20),
                              type_=sa.String(40), existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('ladepunkte', schema=None) as batch_op:
        batch_op.alter_column('plz', existing_type=sa.String(40),
                              type_=sa.String(20), existing_nullable=True)
