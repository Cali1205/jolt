"""Ladepunkte: Betriebszustand, Zugang und Freitexte aufheben.

Der OCM-Import nahm bisher Adresse, Anschluesse, Betreiber und Anzahl - und
warf alles weg, was in Worten dasteht: `UsageCost`, `UsageType`,
`StatusType`, `GeneralComments`, `AccessComments`.

Darin steht aber genau das, was einen Ladepunkt fuer eine konkrete Fahrt
unbrauchbar macht: "nur fuer Hotelgaeste", "hinter Schranke, nachts
geschlossen", "Kabel zu kurz", "Zahlung nur per App", "ausser Betrieb". Ein
Optimierer, der das nicht kennt, plant zuverlaessig Stopps, an denen man
nicht laden kann - und keine noch so gute Zielfunktion gleicht das aus.

Drei der Angaben sind strukturiert genug, um sofort zu filtern:

* `betriebsbereit` - StatusType.IsOperational. **NULL heisst unbekannt**,
  und das ist die Mehrheit; ausgeschlossen wird nur, was ausdruecklich als
  ausser Betrieb gemeldet ist. Wer Unbekanntes ausschliesst, verliert den
  groessten Teil der Datenbank.
* `zugang` - UsageType.Title ("Public", "Private - Restricted access", ...)
* `mitgliedschaft_noetig` - UsageType.IsMembershipRequired

Der Rest ist Freitext und kommt als JSON in `hinweise`. Er wird zunaechst
nur aufgehoben: Ob ihn spaeter ein Sprachmodell in Flags uebersetzt oder ein
paar Regeln reichen, laesst sich erst entscheiden, wenn man ihn hat - und
ihn spaeter nachzuholen hiesse, alles neu zu importieren.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('ladepunkte', schema=None) as batch_op:
        batch_op.add_column(sa.Column('betriebsbereit', sa.Boolean(),
                                      nullable=True))
        batch_op.add_column(sa.Column('zugang', sa.String(60), nullable=True))
        batch_op.add_column(sa.Column('mitgliedschaft_noetig', sa.Boolean(),
                                      nullable=True))
        batch_op.add_column(sa.Column('hinweise', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('ladepunkte', schema=None) as batch_op:
        batch_op.drop_column('hinweise')
        batch_op.drop_column('mitgliedschaft_noetig')
        batch_op.drop_column('zugang')
        batch_op.drop_column('betriebsbereit')
