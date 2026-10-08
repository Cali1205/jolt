"""Vehicles: electricity prices per provider.

The optimizer used to minimise time only. The wish for a particular provider
could therefore only be expressed as a time credit - a preference disguised
as minutes. That made it impossible to formulate the trade-off that is
really at stake: charge longer, but cheaper.

jolt cannot know prices. They depend on the driver's contract, not on the
charge point: the same Ionity charger costs half as much with a Passport
subscription as it does ad hoc. That is why they live on the vehicle and are
maintained by the user.

`strompreise` is a list of {muster, eur_kwh}; as with the preferred
operators, the pattern is compared as a substring ("Ionity" matches
"Ionity GmbH"). `strompreis_eur_kwh` applies to everything that matches no
pattern.

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
