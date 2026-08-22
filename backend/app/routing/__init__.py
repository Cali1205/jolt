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
from .provider import Ort, Route, RoutingFehler, RoutingProvider  # noqa: F401

log = logging.getLogger("uvicorn.error")
_gewarnt = False


def provider():
    global _gewarnt
    if os.environ.get("ORS_API_KEY"):
        return ORS()
    if not _gewarnt:
        log.warning("Kein ORS_API_KEY gesetzt - jolt rechnet mit erfundenen "
                    "Demo-Routen. Kostenloser Schlüssel: openrouteservice.org/dev")
        _gewarnt = True
    return DemoRouting()


def ist_demo(p=None) -> bool:
    return getattr(p or provider(), "ist_demo", False)
