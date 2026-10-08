"""Live replanning: time factor, stored plan, off-route timestamp.

Stage 3 needs three things the session did not record so far:

- `zeitfaktor` - the consumption factor alone does not see a traffic jam.
- `plan` - the currently valid charging plan, so that changes can be
  recognised as changes at all.
- `abweg_seit` - "more than 500 m for more than a minute" needs a
  timestamp, otherwise every inaccurate GPS reading is a replan.

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
    # batch_alter_table, so that the revision also runs on SQLite: there is
    # no ALTER TABLE ADD COLUMN with a default; the table is copied.
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
