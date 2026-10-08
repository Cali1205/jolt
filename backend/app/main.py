"""jolt - App-Zusammenbau: Schema, Router, Frontend.

Die Endpunkte liegen in app/routers/, das Verbrauchsmodell in app/energie/,
die Ladelogik in app/laden/, die Live-Nachführung in app/live/.
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

# Auf einem öffentlich erreichbaren Host legt die interaktive API-Doku die
# gesamte Angriffsfläche offen. Standard daher: aus.
_docs = os.environ.get("ENABLE_API_DOCS", "").lower() in ("1", "true", "yes")

app = FastAPI(title="jolt",
              description="Routenplaner für Elektroautos mit Live-Nachführung",
              docs_url="/api/docs" if _docs else None,
              redoc_url="/api/redoc" if _docs else None,
              openapi_url="/api/openapi.json" if _docs else None)

app.add_middleware(SecurityMiddleware)


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, failure: RequestValidationError):
    """422 ohne den Eingabewert.

    Pydantic hängt jedem Fehler die abgelehnte Eingabe an. Ist das `NaN` oder
    `Infinity` (Pythons JSON-Parser nimmt beides an), scheitert FastAPIs
    Standardantwort beim Serialisieren - der Client bekam einen 500er, und
    zwar ausgerechnet für die Eingabe, die abgelehnt werden sollte. Ohnehin
    gehört der Wert nicht in die Antwort: Er stünde so im Protokoll jedes
    Proxys, auch wenn er ein Passwort-Feld war.
    """
    return JSONResponse(
        {"detail": [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""),
                     "type": e.get("type", "")} for e in failure.errors()]},
        status_code=422)

for router in ALL_ROUTER:
    app.include_router(router)


# Starke Referenz: Die Ereignisschleife hält Aufgaben nur schwach.
_tasks: set = set()


@app.on_event("startup")
def _at_start():
    deps.at_start_warn()
    push.at_start_warn()
    if routing.is_demo():
        log.warning("jolt läuft mit Demo-Routing - die Routen sind erfunden.")
    # Vergessene Fahrten selbst beenden. Für eine Aufzeichnung ist das
    # Vergessen ein Totalverlust: Strecke und Energieprofil entstehen erst
    # beim Beenden aus den Messpunkten.
    _tasks.add(asyncio.create_task(cleanup.loop(SessionLocal)))


# ---------- Frontend ausliefern ----------

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if not os.path.isdir(FRONTEND):
    FRONTEND = "/srv/frontend"       # Pfad im Docker-Image

# index.html und sw.js nie cachen: sonst hängen Clients - allen voran
# iOS-PWAs - auf alten Versionen fest.
WITHOUT_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def _with_version(html: str, files) -> str:
    """Verweise auf eigene Dateien mit ihrer Änderungszeit versehen.

    Der Kern des Problems, das hier viermal zugeschlagen hat: index.html
    wird nie zwischengespeichert (`OHNE_CACHE`, und Cloudflare behandelt sie
    als dynamisch), die Dateien unter `/static` aber sehr wohl - Cloudflare
    ersetzt dort das `no-cache` des Ursprungs durch `max-age=14400`. Der
    Browser holt also frisches HTML und **fragt für das JavaScript gar nicht
    erst nach**. Vier Stunden lang sieht man neue Oberfläche mit alter Logik,
    und der Fehler sieht aus wie ein Bug im frisch geschriebenen Code.

    Mit der Änderungszeit im Verweis bricht die Kette an der einzigen
    Stelle, an der sie zu brechen ist: Ändert sich die Datei, ändert sich
    die Adresse, und eine unbekannte Adresse muss der Browser holen. Ändert
    sie sich nicht, bleibt der Cache gültig und spart weiter Bandbreite.

    Von Hand hochgezählte Versionsnummern kämen dafür nicht in Frage - man
    vergisst sie genau dann, wenn es darauf ankommt.
    """
    for name in files:
        try:
            brand = int(os.path.getmtime(os.path.join(FRONTEND, name)))
        except OSError:
            continue
        html = html.replace(f"/static/{name}", f"/static/{name}?v={brand}")
    return html


# Die Dateien, die index.html einbindet. Ausdrücklich aufgezählt und nicht
# aus dem HTML geraten: Ein Suchausdruck über fremden Text ist genau die
# Sorte Findigkeit, die beim nächsten Umbau still danebenliegt.
INDEX_FILES = ("ble-plugin.js", "obd-ble-native.js", "readings.js",
                 "obd-core.js",
                 "core.js", "map.js", "route.js", "tiles.js", "display.js", "live.js",
                 "settings.js",
                 "trips.js", "vehicle.js", "app.js")


def _code_as_of() -> int:
    """Wann der Code entstanden ist, den dieser Prozess ausliefert.

    Nicht der Zeitpunkt des Bauens, sondern die juengste Aenderungszeit
    unter `frontend/` und `app/` - `COPY` uebernimmt sie ins Image. Der
    Umweg ist Absicht: Ein beim Bauen eingestempeltes Datum haengt daran,
    dass jeder Bauweg es mitgibt, und genau das haelt nicht. Die Dateien
    liegen ohnehin da und luegen nicht.

    Einmal beim Start bestimmt: In einem laufenden Container aendert sich
    keine dieser Dateien mehr, und die Zeile soll jede Anfrage billig sein.
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
    """Die Fassung in die Kopfzeile setzen.

    Zwei Zahlen, weil sie zwei verschiedene Fragen beantworten: Der
    Code-Stand sagt, **was** laeuft, der Serverstart sagt, **seit wann**.
    Steht nach einem Deploy der alte Code-Stand da, ist das Image nicht neu
    gebaut worden; steht der neue da und das Telefon zeigt trotzdem den
    alten, hat der Browser die Seite aus dem Cache.

    Genau diese Unterscheidung hat mich am 2. September zwei falsche
    Schluesse gekostet - ein Fix lag eine Woche undeployt herum, weil von
    aussen nicht zu sehen war, welche Fassung lief.

    Die Zahlen gehen als Sekunden hinaus, nicht als fertiger Text: Der
    Container laeuft in UTC, das Telefon in seiner eigenen Zone, und eine
    Anzeige, die Zweifel ausraeumen soll, darf nicht zwei Stunden neben der
    Uhr des Betrachters liegen. Formatiert wird in app.js; der Text hier
    ist nur der Rueckfall ohne JavaScript und deshalb als UTC benannt.
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
    """Die OBD2-Diagnoseseite - bewusst **nicht** unter /static erreichbar.

    Alles unter /static bekommt von Cloudflare eine Browser-Frist von vier
    Stunden aufgedrückt: Die Einstellung "Browser Cache TTL" überschreibt das
    `no-cache` des Ursprungs, und der Edge-Cache revalidiert zwar, das
    Telefon aber nicht. Für eine Seite, an der jemand im Auto sitzt und beim
    Fehlersuchen alle zehn Minuten eine neue Fassung braucht, ist das
    unbrauchbar - man ändert etwas, es passiert nichts, und die Suche geht in
    die falsche Richtung. Ausserhalb von /static behandelt Cloudflare sie wie
    index.html: dynamisch.

    Die Verweise auf Skript und Stylesheet bekommen die Änderungszeit der
    jeweiligen Datei angehängt. Eine frische Seite zieht damit zwingend
    frische Dateien nach, ohne dass jemand eine Versionsnummer von Hand
    hochzählt - und ohne dass die Dateien selbst /static verlassen müssten.
    """
    with open(os.path.join(FRONTEND, "obd.html"), encoding="utf-8") as file:
        html = _with_version(file.read(), ("ble-plugin.js", "obd-ble-native.js",
                                           "readings.js", "obd-core.js",
                                           "obd.js", "obd.css"))
    # Dieselbe Marke sichtbar auf der Seite: Zweimal war "welche Fassung ist
    # das eigentlich" die Antwort auf einen vermeintlichen Bluetooth-Fehler,
    # und beide Male liess sich das nur mühsam von aussen feststellen.
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
    """Die alte Adresse der Diagnoseseite - leitet auf /obd um.

    Sie muss verschwinden, nicht nur veraltet sein: Unter /static liegt sie
    im Geltungsbereich der Cloudflare-Browserfrist, **und** sie verweist auf
    `/static/obd.js` ohne Versionsparameter. Wer sie aus dem Verlauf oder
    einem Lesezeichen öffnet, bekommt deshalb zuverlässig altes JavaScript -
    und damit Fehler, die längst behoben sind. Genau das ist passiert: Der
    Bluefy-Fehler "Request payload could not be parsed" kam wieder, zwanzig
    Minuten nachdem dieselbe Seite unter /obd funktioniert hatte.

    Vorübergehende Umleitung und ausdrücklich ohne Cache: Eine dauerhafte
    (308) merkt sich der Browser und liesse sich später nicht mehr
    zurücknehmen.
    """
    return RedirectResponse("/obd", status_code=307, headers=WITHOUT_CACHE)


@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(FRONTEND, "sw.js"),
                        media_type="application/javascript", headers=WITHOUT_CACHE)


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


@app.get("/manifest-obd.json")
def manifest_obd():
    """Ein eigenes Manifest fuer die Aufzeichnungsseite.

    Es hat `start_url: /obd`, damit die Seite als eigenes Symbol auf dem
    Home-Bildschirm landet und mit einem Tippen aufgeht - ohne Bluefy, ohne
    Adresszeile, ohne den Umweg ueber die Hauptoberflaeche. Genau das ist
    der Unterschied zwischen "ich zeichne die Fahrt auf" und "ich mache das
    naechstes Mal".
    """
    return FileResponse(os.path.join(FRONTEND, "manifest-obd.json"),
                        media_type="application/manifest+json")


class StaticWithoutStaleCache(StaticFiles):
    """Statische Dateien mit Revalidierung statt blindem Cachen.

    Ohne Cache-Control am Ursprung setzt ein CDN seine eigene Vorgabe - bei
    Cloudflare vier Stunden für .js und .css. Ein Frontend-Deploy ist dann
    unsichtbar, bis diese Frist abläuft: Die Oberfläche lädt neues HTML, dazu
    aber zwei Tage altes JavaScript, und der Fehler sieht aus wie ein Bug im
    Code statt wie ein Cache-Treffer. Genau das ist hier passiert.

    `no-cache` heisst nicht "nicht speichern", sondern "vor Gebrauch
    nachfragen". StaticFiles liefert ETag und Last-Modified mit, die Rückfrage
    endet also im Normalfall bei 304 ohne Inhalt - der Bandbreitenvorteil
    bleibt, die Fehlerklasse verschwindet. Für das Gerüst offline zu halten
    ist ohnehin der Service Worker zuständig, nicht das CDN.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", StaticWithoutStaleCache(directory=FRONTEND), name="static")
