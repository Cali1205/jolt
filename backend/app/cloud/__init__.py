"""Optional vehicle data from a manufacturer cloud.

A source is switched on by its environment variables and by nothing else.
Without them no background task starts and no request leaves the server; the
API then says `configured: false`. Today there is one source, the VW EU Data
Act portal (docs/vw-portal.md). Another brand is one more entry in SOURCES.
"""
import os
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from .snapshot import Snapshot

DEFAULT_INTERVAL_MIN = 15
MIN_INTERVAL_MIN = 5


class Client(Protocol):
    def vehicles(self) -> list[str]: ...

    def latest(self, vin: str) -> Snapshot | None: ...


@dataclass(frozen=True)
class Config:
    name: str
    # The car at the cloud; None means "the only one on the account".
    vin: str | None
    # The jolt vehicle id as configured; None means "the only one in jolt".
    # Checked on the first poll, where a mistake shows up in the status.
    vehicle: str | None
    interval_min: int
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

    def connect() -> Client:
        from .vw_portal import VwPortal
        return VwPortal(user, password)

    return Config(name="vw_portal",
                  vin=env.get("VW_PORTAL_VIN", "").strip() or None,
                  vehicle=env.get("VW_PORTAL_VEHICLE", "").strip() or None,
                  interval_min=_interval(env.get("VW_PORTAL_INTERVAL_MIN")),
                  connect=connect)


SOURCES: dict[str, Callable[[Mapping[str, str]], Config | None]] = {
    "vw_portal": _vw_portal,
}


def active(env: Mapping[str, str] | None = None) -> list[Config]:
    env = os.environ if env is None else env
    return [config for make in SOURCES.values()
            if (config := make(env)) is not None]


def configured(env: Mapping[str, str] | None = None) -> bool:
    return bool(active(env))
