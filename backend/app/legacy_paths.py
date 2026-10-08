"""Old German URL paths keep working.

The API paths were translated to English. Phones and loggers that were set up
before (an iOS shortcut posting to `/api/live/melden`, a bookmarked page that
calls `/api/fahrzeuge`) still use the German ones, so incoming requests are
rewritten to the English path before routing. Only fixed path segments are
mapped; ids and tokens are never touched.
"""

SEGMENTS = {
    "saeulen": "chargers", "bestand": "stock", "entlang": "along",
    "belegt": "occupied", "punkt": "point", "punkte": "points",
    "aufzeichnung": "recording", "melden": "report", "ende": "end",
    "simulieren": "simulate", "schluessel": "key", "abo": "subscription",
    "orte": "places", "fahrten": "trips", "ladeplan": "charge-plan",
    "fahrzeuge": "vehicles", "vorlagen": "templates",
}


def modern(path: str) -> str:
    """The English spelling of an API path; other paths come back unchanged."""
    if not path.startswith("/api/"):
        return path
    return "/".join(SEGMENTS.get(part, part) for part in path.split("/"))


class LegacyPaths:
    """ASGI middleware, outermost: everything behind it only sees English paths."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = modern(scope["path"])
            if path != scope["path"]:
                scope = dict(scope, path=path, raw_path=path.encode("utf-8"))
        await self.app(scope, receive, send)
