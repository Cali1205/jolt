"""jolt - App-Zusammenbau: Schema, Router, Frontend.

Die Endpunkte liegen in app/routers/, das Verbrauchsmodell in app/energie/,
die Ladelogik in app/laden/, die Live-Nachführung in app/live/.
"""
import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import deps, routing
from .database import migrate, seed_vorlagen
from .routers import ALLE_ROUTER
from .security import SecurityMiddleware

log = logging.getLogger("uvicorn.error")

migrate()
seed_vorlagen()

# Auf einem öffentlich erreichbaren Host legt die interaktive API-Doku die
# gesamte Angriffsfläche offen. Standard daher: aus.
_docs = os.environ.get("ENABLE_API_DOCS", "").lower() in ("1", "true", "yes")

app = FastAPI(title="jolt",
              description="Routenplaner für Elektroautos mit Live-Nachführung",
              docs_url="/api/docs" if _docs else None,
              redoc_url="/api/redoc" if _docs else None,
              openapi_url="/api/openapi.json" if _docs else None)

app.add_middleware(SecurityMiddleware)

for router in ALLE_ROUTER:
    app.include_router(router)


@app.on_event("startup")
def _beim_start():
    deps.beim_start_warnen()
    if routing.ist_demo():
        log.warning("jolt läuft mit Demo-Routing - die Routen sind erfunden.")


# ---------- Frontend ausliefern ----------

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if not os.path.isdir(FRONTEND):
    FRONTEND = "/srv/frontend"       # Pfad im Docker-Image

# index.html und sw.js nie cachen: sonst hängen Clients - allen voran
# iOS-PWAs - auf alten Versionen fest.
OHNE_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"), headers=OHNE_CACHE)


@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(FRONTEND, "sw.js"),
                        media_type="application/javascript", headers=OHNE_CACHE)


@app.get("/favicon.ico")
def favicon():
    # Browser fragen die Datei ungefragt an; ohne diese Zeile steht in jedem
    # Log eine 404, die nichts bedeutet und echte Fehler überdeckt.
    return FileResponse(os.path.join(FRONTEND, "icon.svg"),
                        media_type="image/svg+xml")


@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(FRONTEND, "manifest.json"),
                        media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
