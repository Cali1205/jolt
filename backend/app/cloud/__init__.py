"""Optional vehicle data from a manufacturer cloud.

A source is switched on by its environment variables (and, for a source
whose tokens rotate, by tokens in `cloud_credentials`) and by nothing else.
Without them no background task starts and no request leaves the server;
the API then says `configured: false`. The sources: the VW EU Data Act
portal (docs/vw-portal.md) and the Stellantis connected-car API
(docs/stellantis.md). Another brand is one more entry in SOURCES
(docs/data-model.md).
"""
import os
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from .snapshot import Snapshot

DEFAULT_INTERVAL_MIN = 15
MIN_INTERVAL_MIN = 5


class SourceError(Exception):
    """Something the owner can act on; the text is safe to log and show."""


def mask(vin: str) -> str:
    return "…" + vin[-4:]


class Client(Protocol):
    def vehicles(self) -> list[str]: ...

    def updates(self, vin: str) -> list[Snapshot]: ...


@dataclass(frozen=True)
class Config:
    name: str
    # Prefix of the source's environment variables, for setup errors.
    env: str
    # The car at the cloud; None means "the only one on the account".
    vin: str | None
    # The jolt vehicle id as configured; None means "the only one in jolt".
    # Checked on the first poll, where a mistake shows up in the status.
    vehicle: str | None
    interval_min: int
    # The catalog signals (app/signals.py) the source can deliver.
    provides: frozenset[str]
    connect: Callable[[], Client] = field(repr=False)


def _interval(text: str | None) -> int:
    try:
        minutes = int(text) if text else DEFAULT_INTERVAL_MIN
    except ValueError:
        minutes = DEFAULT_INTERVAL_MIN
    return max(MIN_INTERVAL_MIN, minutes)


def _vw_portal(env: Mapping[str, str]) -> Config | None:
    user = env.get("VW_PORTAL_USER", "").strip()
    password = env.get("VW_PORTAL_PASSWORD", "")
    if not user or not password:
        return None
    from .snapshot import PROVIDES

    def connect() -> Client:
        from .vw_portal import VwPortal
        return VwPortal(user, password)

    return Config(name="vw_portal", env="VW_PORTAL",
                  vin=env.get("VW_PORTAL_VIN", "").strip() or None,
                  vehicle=env.get("VW_PORTAL_VEHICLE", "").strip() or None,
                  interval_min=_interval(env.get("VW_PORTAL_INTERVAL_MIN")),
                  provides=PROVIDES, connect=connect)


def _stellantis(env: Mapping[str, str]) -> Config | None:
    """On with STELLANTIS_USER and tokens from tools/stellantis_login.py."""
    user = env.get("STELLANTIS_USER", "").strip()
    if not user:
        return None
    from ..database import SessionLocal
    from . import credentials
    from .stellantis import PROVIDES, SOURCE, Stellantis, account_key
    brand = env.get("STELLANTIS_BRAND", "").strip().lower() or "opel"
    db = SessionLocal()
    try:
        if credentials.load(db, SOURCE, account_key(brand, user)) is None:
            return None
    finally:
        db.close()
    return Config(name=SOURCE, env="STELLANTIS",
                  vin=env.get("STELLANTIS_VIN", "").strip() or None,
                  vehicle=env.get("STELLANTIS_VEHICLE", "").strip() or None,
                  interval_min=_interval(env.get("STELLANTIS_INTERVAL_MIN")),
                  provides=PROVIDES,
                  connect=lambda: Stellantis(brand, user, SessionLocal))


SOURCES: dict[str, Callable[[Mapping[str, str]], Config | None]] = {
    "vw_portal": _vw_portal,
    "stellantis": _stellantis,
}


def active(env: Mapping[str, str] | None = None) -> list[Config]:
    env = os.environ if env is None else env
    return [config for make in SOURCES.values()
            if (config := make(env)) is not None]


def configured(env: Mapping[str, str] | None = None) -> bool:
    return bool(active(env))
