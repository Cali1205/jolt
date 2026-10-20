"""Snapshots: stored values in the signal catalog's spelling.

Rows written before the catalog hold VW's own `charge_state` strings and a
`climatisation_state`. Now a source normalises at its boundary, so the rows
are rewritten once: `charge_state` to the catalog enum (an unknown string is
dropped, as reading it did before) and `climatisation_state` to
`climate_state`. The raw points in `raw` stay as they are.

The tables are copied here, not imported: a revision must keep meaning what
it meant when it was written.

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

CHARGE_STATES = {
    "charging": "charging",
    "readyforcharging": "ready",
    "notreadyforcharging": "not_ready",
    "chargepurposereachedandnotconversationcharging": "purpose_reached",
    "chargepurposereachedandnotconservationcharging": "purpose_reached",
    "chargepurposereachedandconversation": "conservation",
    "chargepurposereachedandconservation": "conservation",
    "error": "error",
}
CATALOG = set(CHARGE_STATES.values())
BACK = {
    "charging": "CHARGE_STATE_CHARGING",
    "ready": "CHARGE_STATE_READY_FOR_CHARGING",
    "not_ready": "CHARGE_STATE_NOT_READY_FOR_CHARGING",
    "purpose_reached": "CHARGE_STATE_CHARGE_PURPOSE_REACHED_AND_NOT_CONSERVATION_CHARGING",
    "conservation": "CHARGE_STATE_CHARGE_PURPOSE_REACHED_AND_CONSERVATION",
    "error": "CHARGE_STATE_ERROR",
}

snapshots = sa.table("vehicle_snapshots", sa.column("id", sa.Integer),
                     sa.column("values", sa.JSON))


def _forward(values: dict) -> dict:
    out = dict(values)
    if "charge_state" in out:
        state = out.pop("charge_state")
        if state in CATALOG:
            out["charge_state"] = state
        else:
            key = str(state).strip().lower().replace("_", "").removeprefix("chargestate")
            if key in CHARGE_STATES:
                out["charge_state"] = CHARGE_STATES[key]
    if "climatisation_state" in out:
        climate = str(out.pop("climatisation_state")).strip().lower()
        out["climate_state"] = climate.removeprefix("climatisation_state_")
    return out


def _backward(values: dict) -> dict:
    out = dict(values)
    if out.get("charge_state") in BACK:
        out["charge_state"] = BACK[out["charge_state"]]
    if "climate_state" in out:
        out["climatisation_state"] = ("CLIMATISATION_STATE_"
                                      + str(out.pop("climate_state")).upper())
    return out


def _rewrite(change) -> None:
    connection = op.get_bind()
    for row_id, values in connection.execute(
            sa.select(snapshots.c.id, snapshots.c["values"])).all():
        if not isinstance(values, dict):
            continue
        new = change(values)
        if new != values:
            connection.execute(snapshots.update()
                               .where(snapshots.c.id == row_id)
                               .values({"values": new}))


def upgrade() -> None:
    _rewrite(_forward)


def downgrade() -> None:
    _rewrite(_backward)
