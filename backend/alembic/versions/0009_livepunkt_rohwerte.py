"""Live-Punkte: Rohwerte der Quelle mitschreiben.

`LivePunkt` führt genau das, womit die Nachführung rechnet: Position,
Ladestand, Tempo, Aussentemperatur. Ein OBD2-Logger liefert aber mehr -
Packspannung, Strom, Kilometerstand, Innentemperatur - und vor allem
liefert er den **unverrechneten** Wert, aus dem der Ladestand entstand.

Das ist kein Sammeltrieb, es löst ein konkretes Problem. Der Ladestand des
ID.Buzz kommt als einzelnes Byte, aus dem sich zwei Zahlen ergeben: der
Brutto-Wert der Batterie und der, den die Anzeige zeigt. Die Umrechnung
zwischen beiden ist dokumentiert, aber für die ID.3 - beim ID.Buzz liegt sie
um gut einen Prozentpunkt daneben, und zwar nicht konstant. Um sie zu
berichtigen, braucht es Messpunkte mit dem Rohwert **und** dem, was das
Display in derselben Minute zeigte. Wer das nicht mitschreibt, muss dafür
ein zweites Mal losfahren.

Bewusst JSON und keine Spalten je Messgrösse: Welche Werte eine Quelle
liefert, steht nicht fest und wird sich ändern. Eine Spalte je Grösse hiesse
eine Migration je Datenkennung, die jemand interessant findet. Gerechnet
wird mit diesen Werten nicht - dafür sind die getippten Felder da; hier
liegt, was man später auswerten können will.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rohwerte', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.drop_column('rohwerte')
