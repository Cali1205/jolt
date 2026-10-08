"""jolt - app assembly: schema, routers, frontend.

The endpoints live in app/routers/, the consumption model in app/energy/,
the charging logic in app/charging/, the live tracking in app/live/.
"""
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles

import asyncio

from . import deps, push, routing
from .live import cleanup
from .database import SessionLocal, migrate, seed_templates
from .routers import ALL_ROUTER
from .security import SecurityMiddleware

log = logging.getLogger("uvicorn.error")

migrate()
seed_templates()

# On a publicly reachable host the interactive API documentation exposes the
# entire attack surface. Default is therefore: off.
_docs = os.environ.get("ENABLE_API_DOCS", "").lower() in ("1", "true", "yes")

app = FastAPI(title="jolt",
              description="Routenplaner für Elektroautos mit Live-Nachführung",
              docs_url="/api/docs" if _docs else None,
              redoc_url="/api/redoc" if _docs else None,
              openapi_url="/api/openapi.json" if _docs else None)

app.add_middleware(SecurityMiddleware)


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, failure: RequestValidationError):
    """422 without the input value.

    Pydantic attaches the rejected input to every error. If that is `NaN` or
    `Infinity` (Python's JSON parser accepts both), FastAPI's default response
    fails during serialisation - the client got a 500, and precisely for the
    input that was supposed to be rejected. The value does not belong in the
    response anyway: it would end up in the log of every proxy, even if it was
    a password field.
    """
    return JSONResponse(
        {"detail": [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""),
                     "type": e.get("type", "")} for e in failure.errors()]},
        status_code=422)

for router in ALL_ROUTER:
    app.include_router(router)


# Strong reference: the event loop holds tasks only weakly.
_tasks: set = set()


@app.on_event("startup")
def _at_start():
    deps.at_start_warn()
    push.at_start_warn()
    if routing.is_demo():
        log.warning("jolt is running with demo routing - the routes are made up.")
    # End forgotten trips by ourselves. For a recording, forgetting is a total
    # loss: the route and energy profile are only created from the measurement
    # points when it ends.
    _tasks.add(asyncio.create_task(cleanup.loop(SessionLocal)))


# ---------- Serve the frontend ----------

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if not os.path.isdir(FRONTEND):
    FRONTEND = "/srv/frontend"       # path in the Docker image

# Never cache index.html and sw.js: otherwise clients - above all iOS PWAs -
# get stuck on old versions.
WITHOUT_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def _with_version(html: str, files) -> str:
    """Add their modification time to references to own files.

    The core of the problem that has struck here four times: index.html is
    never cached (`WITHOUT_CACHE`, and Cloudflare treats it as dynamic), but
    the files under `/static` very much are - Cloudflare replaces the origin's
    `no-cache` there with `max-age=14400`. So the browser fetches fresh HTML
    and **does not even ask** for the JavaScript. For four hours you see the
    new interface with old logic, and the error looks like a bug in the freshly
    written code.

    With the modification time in the reference, the chain breaks at the only
    place where it can be broken: if the file changes, the address changes,
    and an unknown address must be fetched by the browser. If it does not
    change, the cache stays valid and keeps saving bandwidth.

    Manually incremented version numbers were out of the question - one
    forgets them exactly when it matters.
    """
    for name in files:
        try:
            brand = int(os.path.getmtime(os.path.join(FRONTEND, name)))
        except OSError:
            continue
        html = html.replace(f"/static/{name}", f"/static/{name}?v={brand}")
    return html


# The files that index.html includes. Listed explicitly and not guessed from
# the HTML: a search expression over foreign text is exactly the kind of
# cleverness that silently misses at the next restructuring.
INDEX_FILES = ("ble-plugin.js", "obd-ble-native.js", "readings.js",
                 "obd-core.js",
                 "core.js", "map.js", "route.js", "tiles.js", "display.js", "live.js",
                 "settings.js",
                 "trips.js", "vehicle.js", "app.js")


def _code_as_of() -> int:
    """When the code was created that this process serves.

    Not the time of building, but the most recent modification time under
    `frontend/` and `app/` - `COPY` carries it into the image. The detour is
    deliberate: a date stamped in at build time depends on every build path
    passing it along, and that does not hold. The files are there anyway and
    do not lie.

    Determined once at startup: in a running container none of these files
    changes any more, and the line should be cheap on every request.
    """
    newest = 0
    for folder in (FRONTEND, os.path.dirname(__file__)):
        for root, _, files in os.walk(folder):
            for name in files:
                if not name.endswith((".py", ".js", ".html", ".css")):
                    continue
                try:
                    newest = max(newest,
                                  int(os.path.getmtime(os.path.join(root, name))))
                except OSError:
                    pass
    return newest


CODE_AS_OF = _code_as_of()
PROCESS_START = int(time.time())


def _as_of_row(html: str) -> str:
    """Put the version into the header line.

    Two numbers, because they answer two different questions: the code state
    says **what** is running, the server start says **since when**. If after a
    deploy the old code state is shown, the image has not been rebuilt; if the
    new one is shown and the phone still displays the old one, the browser has
    taken the page from its cache.

    Exactly this distinction cost me two wrong conclusions on 2 September - a
    fix lay undeployed for a week because it could not be seen from outside
    which version was running.

    The numbers go out as seconds, not as finished text: the container runs in
    UTC, the phone in its own zone, and a display that is meant to remove
    doubt must not be two hours off the viewer's clock. Formatting is done in
    app.js; the text here is only the fallback without JavaScript and is
    therefore labelled as UTC.
    """
    return (html
            .replace("{{STAND_S}}", str(CODE_AS_OF))
            .replace("{{START_S}}", str(PROCESS_START))
            .replace("{{STAND}}",
                     time.strftime("%d.%m. %H:%M UTC",
                                   time.gmtime(CODE_AS_OF))))


@app.get("/")
def index():
    with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as file:
        return HTMLResponse(_as_of_row(_with_version(file.read(),
                                                      INDEX_FILES)),
                            headers=WITHOUT_CACHE)


@app.get("/obd")
def obd_page():
    """The OBD2 diagnostics page - deliberately **not** reachable under /static.

    Everything under /static gets a browser lifetime of four hours imposed by
    Cloudflare: the "Browser Cache TTL" setting overrides the origin's
    `no-cache`, and the edge cache does revalidate, but the phone does not.
    For a page where someone is sitting in the car and needs a new version
    every ten minutes while troubleshooting, that is unusable - you change
    something, nothing happens, and the search goes in the wrong direction.
    Outside /static Cloudflare treats it like index.html: dynamic.

    The references to script and stylesheet get the modification time of the
    respective file appended. A fresh page thus necessarily pulls in fresh
    files, without anyone incrementing a version number by hand - and without
    the files themselves having to leave /static.
    """
    with open(os.path.join(FRONTEND, "obd.html"), encoding="utf-8") as file:
        html = _with_version(file.read(), ("ble-plugin.js", "obd-ble-native.js",
                                           "readings.js", "obd-core.js",
                                           "obd.js", "obd.css"))
    # The same mark visible on the page: twice "which version is this
    # actually" was the answer to a supposed Bluetooth error, and both times it
    # could only be established from outside with difficulty.
    newest = 0
    for name in ("obd.html", "ble-plugin.js", "obd-ble-native.js",
                 "readings.js", "obd-core.js", "obd.js", "obd.css"):
        try:
            newest = max(newest,
                          int(os.path.getmtime(os.path.join(FRONTEND, name))))
        except OSError:
            pass
    html = html.replace("STAND", time.strftime("%d.%m. %H:%M",
                                               time.localtime(newest)))
    return HTMLResponse(html, headers=WITHOUT_CACHE)


@app.get("/static/obd.html")
def obd_old_address():
    """The old address of the diagnostics page - redirects to /obd.

    It must disappear, not merely be outdated: under /static it lies within
    the scope of the Cloudflare browser lifetime, **and** it refers to
    `/static/obd.js` without a version parameter. Anyone who opens it from the
    history or a bookmark therefore reliably gets old JavaScript - and with it
    errors that were fixed long ago. That is exactly what happened: the
    Bluefy error "Request payload could not be parsed" came back twenty
    minutes after the same page had worked under /obd.

    Temporary redirect and explicitly without cache: a permanent one (308) is
    remembered by the browser and could not be taken back later.
    """
    return RedirectResponse("/obd", status_code=307, headers=WITHOUT_CACHE)


@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(FRONTEND, "sw.js"),
                        media_type="application/javascript", headers=WITHOUT_CACHE)


@app.get("/favicon.ico")
def favicon():
    # Browsers request the file unasked; without this line every log contains
    # a 404 that means nothing and covers up real errors.
    return FileResponse(os.path.join(FRONTEND, "icon.svg"),
                        media_type="image/svg+xml")


@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(FRONTEND, "manifest.json"),
                        media_type="application/manifest+json")


@app.get("/manifest-obd.json")
def manifest_obd():
    """A manifest of its own for the recording page.

    It has `start_url: /obd`, so that the page ends up as an icon of its own
    on the home screen and opens with a tap - without Bluefy, without an
    address bar, without the detour via the main interface. That is exactly
    the difference between "I am recording the trip" and "I will do that next
    time".
    """
    return FileResponse(os.path.join(FRONTEND, "manifest-obd.json"),
                        media_type="application/manifest+json")


class StaticWithoutStaleCache(StaticFiles):
    """Static files with revalidation instead of blind caching.

    Without Cache-Control at the origin, a CDN applies its own default - with
    Cloudflare four hours for .js and .css. A frontend deploy is then
    invisible until that period expires: the interface loads new HTML, but
    with it two-day-old JavaScript, and the error looks like a bug in the code
    instead of a cache hit. That is exactly what happened here.

    `no-cache` does not mean "do not store", but "ask before use".
    StaticFiles delivers ETag and Last-Modified, so the revalidation normally
    ends in a 304 without content - the bandwidth benefit stays, the class of
    error disappears. Keeping the shell available offline is the job of the
    service worker anyway, not the CDN.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", StaticWithoutStaleCache(directory=FRONTEND), name="static")
