"""Keep the battery capacity reported by the vehicle.

The vehicle profile holds a brochure figure - 77 kWh net for the ID.Buzz.
The vehicle itself reports a different number via `222AB2`: 73.8 kWh was
measured at 59,581 km, i.e. 96 % of it. The difference is not measurement
inaccuracy but ageing, and it grows over the years.

Why this is more than curiosity: **every** conversion between state of
charge and kilowatt-hours hangs on this number. The measured consumption of
a recording, the correction factor learned from it, the charging
increments in the charging plan and the remaining range - all compute
`percent times capacity`. Four percent too much capacity means four percent
too much assumed energy, consistently in the same direction.

Two columns instead of one: without the timestamp nobody knows whether the
number is from yesterday or from the year before last - and a value whose
age is unknown is worth little for a slowly drifting quantity.

Both are NULL as long as nothing has been measured. NULL here means "no
measurement", not "zero kWh"; the evaluation then falls back to the
profile.
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
