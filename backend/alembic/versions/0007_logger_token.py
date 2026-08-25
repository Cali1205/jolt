"""Fahrzeuge: langlebiges Token für einen Logger im Auto.

Messpunkte kamen bisher ausschliesslich über `/api/live/{sitzung_id}/punkt`
herein. Das passt zur PWA, die die Fahrt selbst gestartet hat und die ID
deshalb kennt - aber nicht zu einem Gerät, das im Auto verbaut ist und beim
Anschalten einfach zu senden beginnt. Die Sitzungs-ID wechselt mit jeder
Fahrt; ein Dongle kann sie nicht wissen.

Mit diesem Token meldet sich stattdessen das *Fahrzeug*, und das Backend
sucht dessen laufende Sitzung. NULL heisst "kein Logger eingerichtet".

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fahrzeuge', schema=None) as batch_op:
        batch_op.add_column(sa.Column('logger_token', sa.String(64),
                                      nullable=True))
        # Eindeutig, damit ein Token nie zwei Fahrzeuge trifft. Mehrere NULL
        # sind davon unberührt - Fahrzeuge ohne Logger stören einander nicht.
        batch_op.create_index('ix_fahrzeuge_logger_token', ['logger_token'],
                              unique=True)


def downgrade() -> None:
    with op.batch_alter_table('fahrzeuge', schema=None) as batch_op:
        batch_op.drop_index('ix_fahrzeuge_logger_token')
        batch_op.drop_column('logger_token')
