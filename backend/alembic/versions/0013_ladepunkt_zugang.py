"""Charge points: keep operational status, access and free-text notes.

The OCM import used to take address, connectors, operator and count - and
discarded everything stated in words: `UsageCost`, `UsageType`,
`StatusType`, `GeneralComments`, `AccessComments`.

That is exactly what makes a charge point unusable for a concrete trip:
"hotel guests only", "behind a barrier, closed at night", "cable too short",
"payment by app only", "out of order". An optimizer that does not know this
reliably plans stops where you cannot charge - and no objective function,
however good, makes up for that.

Three of the details are structured enough to filter on immediately:

* `betriebsbereit` - StatusType.IsOperational. **NULL means unknown**, and
  that is the majority; only what is explicitly reported as out of order is
  excluded. Excluding the unknown loses most of the database.
* `zugang` - UsageType.Title ("Public", "Private - Restricted access", ...)
* `mitgliedschaft_noetig` - UsageType.IsMembershipRequired

The rest is free text and goes into `hinweise` as JSON. For now it is only
kept: whether a language model later translates it into flags or a few
rules suffice can only be decided once you have it - and catching it up
later would mean re-importing everything.

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
