"""The vehicle identification number of a vehicle.

So the app can tell, from what the dongle reads, which car it is plugged
into. Filled by hand, by a manufacturer cloud that knows the car, or once
from the dongle; NULL until one of them knows it.

Revision ID: 0029
Revises: 0028
"""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("vin", sa.String(17), nullable=True))
        batch_op.create_index("ix_vehicles_vin", ["vin"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("vehicles", schema=None) as batch_op:
        batch_op.drop_index("ix_vehicles_vin")
        batch_op.drop_column("vin")
