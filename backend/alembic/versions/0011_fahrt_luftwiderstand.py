"""Fahrten: Zuschlag auf den Luftwiderstand.

Ein Fahrradträger auf der Anhängerkupplung oder eine Dachbox gehören zur
**Fahrt**, nicht zum Fahrzeug - wie die Zuladung. Ihre Masse liesse sich
schon über `zuladung_kg` abbilden, aber sie ist der kleinere Posten: Bei
Autobahntempo kostet ein Träger vor allem Luftwiderstand, und der geht mit
dem Quadrat der Geschwindigkeit.

Der eigentliche Grund, ihn zu modellieren, ist aber ein anderer: Ohne ihn
landet sein Verbrauch im **Korrekturfaktor des Fahrzeugs** - und der gilt
dauerhaft und für alle Fahrten. Eine einzige Urlaubsfahrt mit Trägern würde
die Alltagsplanung verbiegen. Genau davor warnt der Kommentar in
`energie/kalibrierung.py` schon ("eine Fahrt mit unbemerkter Dachbox").

1.0 heisst "nichts dran". Der Wert multipliziert den Luftwiderstandsbeiwert;
was er wirklich sein muss, weiss man erst nach einer aufgezeichneten Fahrt
mit Träger.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.add_column(sa.Column('luftwiderstand_faktor', sa.Float(),
                                      nullable=False, server_default='1.0'))


def downgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.drop_column('luftwiderstand_faktor')
