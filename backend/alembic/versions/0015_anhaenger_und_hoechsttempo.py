"""Trailer and top speed.

The planning speed slider has no absolute upper limit: at 130 % the model
calculates with 165 km/h, which no production vehicle with this drag
drives. For a towing combination, 100 km/h is also not a preference but a
hard limit.

On the **vehicle** the top speed, on the **trip** the trailer (mass,
additional drag area) and a trip-specific limit. All NULL as long as
nothing is set - existing trips and vehicles calculate exactly as before.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("fahrzeuge", schema=None) as batch_op:
        batch_op.add_column(sa.Column("max_tempo_kmh", sa.Float(), nullable=True))
    with op.batch_alter_table("fahrten", schema=None) as batch_op:
        batch_op.add_column(sa.Column("anhaenger_kg", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("anhaenger_cwa_m2", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("tempo_max_kmh", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("fahrten", schema=None) as batch_op:
        batch_op.drop_column("tempo_max_kmh")
        batch_op.drop_column("anhaenger_cwa_m2")
        batch_op.drop_column("anhaenger_kg")
    with op.batch_alter_table("fahrzeuge", schema=None) as batch_op:
        batch_op.drop_column("max_tempo_kmh")
