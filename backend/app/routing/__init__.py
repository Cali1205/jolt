"""Selection of the routing adapter.

Rule: if an ORS key is present, real routing is used. If it is missing, demo
routing steps in so that the app starts at all and can be clicked through.
The fallback is recognizable in every response (`demo: true`) - an invented
route must never be mistaken for a real one unnoticed.
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
        log.warning("No ORS_API_KEY set - jolt is using invented "
                    "demo routes. Free key: openrouteservice.org/dev")
        _warned = True
    return DemoRouting()


def is_demo(p=None) -> bool:
    return getattr(p or provider(), "is_demo", False)
