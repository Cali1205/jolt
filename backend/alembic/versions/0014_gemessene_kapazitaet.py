"""Die vom Fahrzeug gemeldete Akkukapazitaet aufheben.

Im Fahrzeugprofil steht eine Prospektangabe - beim ID.Buzz 77 kWh netto.
Das Fahrzeug selbst meldet ueber `222AB2` eine andere Zahl: gemessen wurden
73,8 kWh bei 59 581 km, also 96 % davon. Der Unterschied ist keine
Messungenauigkeit, sondern Alterung, und er waechst mit den Jahren.

Warum das mehr ist als Neugier: An dieser Zahl haengt **jede** Umrechnung
zwischen Ladestand und Kilowattstunden. Der gemessene Verbrauch einer
Aufzeichnung, der daraus gelernte Korrekturfaktor, die Ladehuebe im
Ladeplan und die Restreichweite - alle rechnen `Prozent mal Kapazitaet`.
Vier Prozent zu viel Kapazitaet heissen vier Prozent zu viel angenommene
Energie, und zwar durchgaengig in dieselbe Richtung.

Zwei Spalten statt einer: Ohne den Zeitpunkt weiss niemand, ob die Zahl von
gestern oder von vorletztem Jahr stammt - und ein Wert, dessen Alter man
nicht kennt, ist bei einer langsam wandernden Groesse wenig wert.

Beide sind NULL, solange nichts gemessen wurde. NULL heisst hier "keine
Messung", nicht "null kWh"; die Auswertung faellt dann auf das Profil
zurueck.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fahrzeuge",
                  sa.Column("gemessene_kapazitaet_kwh", sa.Float(),
                            nullable=True))
    op.add_column("fahrzeuge",
                  sa.Column("kapazitaet_gemessen_am", sa.DateTime(),
                            nullable=True))


def downgrade() -> None:
    op.drop_column("fahrzeuge", "kapazitaet_gemessen_am")
    op.drop_column("fahrzeuge", "gemessene_kapazitaet_kwh")
