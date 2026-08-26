"""Fahrten: aufgezeichnet statt geplant.

Bisher entstand eine Fahrt immer aus einer Planung - Start, Ziel, Route vom
Routing-Dienst, Profil gerechnet. Der umgekehrte Weg fehlte: losfahren,
mitschreiben, und die Strecke hinterher aus dem entstehen lassen, was
aufgezeichnet wurde.

Gebraucht wird er für die Kalibrierung. Eine bekannte kurze Strecke, immer
dieselbe, ein paarmal gefahren, ist die sauberste Verbrauchsmessung
überhaupt - und dafür erst eine Route planen zu müssen, ist umständlich
genug, dass man es bleiben lässt.

Das Flag unterscheidet die beiden: Bei einer Aufzeichnung sind Geometrie und
Energieprofil zu Beginn leer und werden beim Beenden gebaut (siehe
`live/aufzeichnung.py`).

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.add_column(sa.Column('aufzeichnung', sa.Boolean(),
                                      nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('fahrten', schema=None) as batch_op:
        batch_op.drop_column('aufzeichnung')
