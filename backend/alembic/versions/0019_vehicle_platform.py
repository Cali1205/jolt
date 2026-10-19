"""Which OBD2 platform profile a vehicle uses.

The dongle code used to be hard-wired to the VW MEB. The profiles now live in
frontend/platforms/, and each vehicle names the one it needs. The column
holds only the id; which ids exist is the frontend's knowledge.

Existing vehicles get "meb": until now that is the only thing the dongle
could talk to, so every recorded trip was a MEB trip.

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("platform", sa.String(length=20),
                                      nullable=False, server_default="meb"))


def downgrade() -> None:
    with op.batch_alter_table("vehicles", schema=None) as batch_op:
        batch_op.drop_column("platform")
