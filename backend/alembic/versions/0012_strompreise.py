"""Fahrzeuge: Strompreise je Anbieter.

Der Optimierer minimierte bisher ausschliesslich Zeit. Der Wunsch nach einem
bestimmten Anbieter war deshalb nur als Zeitgutschrift auszudrücken - eine
Vorliebe, als Minuten verkleidet. Damit liess sich der Handel, um den es
wirklich geht, gar nicht formulieren: laenger laden, dafuer billiger.

Preise kann jolt nicht wissen. Sie haengen am Vertrag des Fahrers, nicht am
Ladepunkt: Dieselbe Ionity-Saeule kostet mit Passport-Abo die Haelfte von
dem, was sie ad hoc kostet. Deshalb stehen sie am Fahrzeug und werden vom
Nutzer gepflegt.

`strompreise` ist eine Liste von {muster, eur_kwh}; das Muster wird wie bei
den bevorzugten Betreibern als Teilzeichenkette verglichen ("Ionity" trifft
"Ionity GmbH"). `strompreis_eur_kwh` gilt fuer alles, was auf kein Muster
passt.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fahrzeuge', schema=None) as batch_op:
        batch_op.add_column(sa.Column('strompreis_eur_kwh', sa.Float(),
                                      nullable=False, server_default='0.59'))
        batch_op.add_column(sa.Column('strompreise', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('fahrzeuge', schema=None) as batch_op:
        batch_op.drop_column('strompreise')
        batch_op.drop_column('strompreis_eur_kwh')
