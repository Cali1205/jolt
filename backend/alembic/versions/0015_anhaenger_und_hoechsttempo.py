"""Anhänger und Höchstgeschwindigkeit.

Der Tempo-Regler der Planung hat keine absolute Obergrenze: Bei 130 % rechnet
das Modell mit 165 km/h, die kein Serienfahrzeug mit diesem Luftwiderstand
fährt. Für ein Gespann ist 100 km/h ausserdem keine Vorliebe, sondern eine
harte Grenze.

Am **Fahrzeug** die Höchstgeschwindigkeit, an der **Fahrt** der Anhänger
(Masse, zusätzliche Luftwiderstandsfläche) und eine fahrtbezogene Grenze.
Alles NULL, solange nichts gesetzt ist - bestehende Fahrten und Fahrzeuge
rechnen genau wie vorher.

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
