"""Live-Umplanung: Zeitfaktor, gespeicherter Plan, Abweg-Zeitstempel.

Stufe 3 braucht drei Dinge, die die Sitzung bisher nicht mitschrieb:

- `zeitfaktor` - der Verbrauchsfaktor allein sieht einen Stau nicht.
- `plan` - der aktuell gueltige Ladeplan, um Aenderungen ueberhaupt als
  Aenderung erkennen zu koennen.
- `abweg_seit` - "mehr als 500 m fuer mehr als eine Minute" braucht einen
  Zeitstempel, sonst ist jede ungenaue GPS-Messung eine Neuplanung.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table, damit die Revision auch auf SQLite laeuft: Dort gibt
    # es kein ALTER TABLE ADD COLUMN mit Default, die Tabelle wird kopiert.
    with op.batch_alter_table('live_sitzungen', schema=None) as batch_op:
        batch_op.add_column(sa.Column('zeitfaktor', sa.Float(), nullable=False,
                                      server_default='1.0'))
        batch_op.add_column(sa.Column('plan', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('abweg_seit', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('live_sitzungen', schema=None) as batch_op:
        batch_op.drop_column('abweg_seit')
        batch_op.drop_column('plan')
        batch_op.drop_column('zeitfaktor')
