"""Live-Punkte: Ladestand darf fehlen.

Bisher musste jeder Messpunkt einen Ladestand tragen. Das erzwang genau das
Verhalten, das die Nachführung unbrauchbar macht: Die PWA meldete nur dann
etwas, wenn jemand den Ladestand eintippte - also an Ladestopps, und
dazwischen zwei Stunden lang gar nichts. Ohne Punkte gibt es aber auch keine
Position und keine Zeit, und damit steht der Zeitfaktor die ganze Fahrt auf
1,0 und die Ankunftsprognose auf dem Stand der Abfahrt.

Das Auto weiss seinen Ladestand nicht von selbst, seine Position aber sehr
wohl - das Telefon liefert sie im Sekundentakt und umsonst. Die beiden
Grössen haben verschiedene Taktraten, und das Schema muss das zulassen:
Position dauernd, Ladestand gelegentlich.

Was zwischen zwei gemeldeten Ladeständen gilt, rechnet `live/sitzung.py`
aus dem Energieprofil hoch - das kennt Steigung, Tempo und Wetter der
Strecke. Genau dafür ist es da.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.alter_column('soc', existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    # Punkte ohne Ladestand müssen weg, bevor die Spalte wieder NOT NULL wird -
    # sonst scheitert die Rückmigration an genau den Daten, die sie erzeugt hat.
    op.execute('DELETE FROM live_punkte WHERE soc IS NULL')
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.alter_column('soc', existing_type=sa.Float(), nullable=False)
