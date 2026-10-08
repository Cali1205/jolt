"""Auswahl des Routing-Adapters.

Regel: Liegt ein ORS-Schlüssel vor, wird echt geroutet. Fehlt er, springt das
Demo-Routing ein, damit die App überhaupt startet und sich durchklicken lässt.
Der Fallback ist an jeder Antwort erkennbar (`demo: true`) - eine erfundene
Route darf nie unbemerkt für eine echte gehalten werden.
"""
import logging
import os

from .demo import DemoRouting
from .ors import ORS
from .provider import City, Route, RoutingError, RoutingProvider  # noqa: F401

log = logging.getLogger("uvicorn.error")
_warned = False


def provider():
    global _warned
    if os.environ.get("ORS_API_KEY"):
        return ORS()
    if not _warned:
        log.warning("Kein ORS_API_KEY gesetzt - jolt rechnet mit erfundenen "
                    "Demo-Routen. Kostenloser Schlüssel: openrouteservice.org/dev")
        _warned = True
    return DemoRouting()


def is_demo(p=None) -> bool:
    return getattr(p or provider(), "is_demo", False)
