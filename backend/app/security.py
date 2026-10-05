"""Härtung für den Betrieb hinter einem Reverse Proxy.

Enthält Security-Header und ein IP-basiertes Rate-Limit. Bewusst ohne externe
Abhängigkeit: Eine Instanz mit einer Handvoll Geräten kommt mit einem Zähler
im Speicher aus, und der kann nicht selbst ausfallen.
"""
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Nur Proxys aus dieser Liste dürfen die Client-IP setzen. Ohne die Prüfung
# könnte jeder Client den Header fälschen und das Rate-Limit umgehen.
TRUSTED_PROXIES = {p.strip() for p in
                   os.environ.get("TRUSTED_PROXIES", "").split(",") if p.strip()}

GLOBAL_WINDOW = 60
GLOBAL_MAX = int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))
LOGIN_WINDOW = 15 * 60
LOGIN_MAX = int(os.environ.get("LOGIN_LIMIT_PER_15MIN", "10"))

_sperre = threading.Lock()
_treffer: dict[str, deque] = defaultdict(deque)
_login_treffer: dict[str, deque] = defaultdict(deque)

# Der Live-Endpunkt bekommt im Sekundentakt Messpunkte. Ein Limit von 120
# Anfragen je Minute wäre dafür genau falsch: Es würde ausgerechnet die
# Funktion abwürgen, um die es geht.
AUSGENOMMEN = ("/api/live/",)

CSP = ("default-src 'self'; "
       # Kartenkacheln kommen vom OSM-Tileserver, sonst bliebe die Karte leer.
       # Beide Schreibweisen: der kanonische Host hat keine Subdomain und
       # würde von "*.tile.openstreetmap.org" allein nicht erfasst.
       "img-src 'self' data: blob: https://tile.openstreetmap.org "
       "https://*.tile.openstreetmap.org; "
       "style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; "
       "connect-src 'self' ws: wss:; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def client_ip(request: Request) -> str:
    """Echte Client-IP ermitteln.

    Bei Docker-Port-Publishing sieht der Container sonst nur das Gateway -
    alle Nutzer teilten sich dann einen Zähler.
    """
    peer = request.client.host if request.client else "unbekannt"
    if peer in TRUSTED_PROXIES:
        weitergereicht = request.headers.get("x-forwarded-for", "")
        if weitergereicht:
            return weitergereicht.split(",")[0].strip()
    return peer


def _verfallen(warteschlange: deque, jetzt: float, fenster: int) -> None:
    while warteschlange and jetzt - warteschlange[0] > fenster:
        warteschlange.popleft()


# Wie oft abgelaufene Absender aus dem Speicher genommen werden. Der Zähler
# legt je Absender-IP einen Eintrag an; ohne Aufräumen wüchse er mit jeder IP,
# die je angefragt hat - und hinter einem Proxy, der den Header nicht
# überschreibt, mit jedem erfundenen Wert.
AUFRAEUMEN_ALLE_S = 60
_zuletzt_aufgeraeumt: dict[int, float] = {}


def _zaehlen(eimer: dict, schluessel: str, fenster: int, grenze: int,
             aufraeumen_ab: float | None = None) -> bool:
    """True, wenn die Anfrage erlaubt ist.

    `aufraeumen_ab` gibt es nur für die Prüfung: Zeitpunkt der letzten
    Aufräumrunde, damit sich die Runde ohne Warten auslösen lässt.
    """
    jetzt = time.time()
    with _sperre:
        _abgelaufene_entfernen(eimer, jetzt, fenster, aufraeumen_ab)
        warteschlange = eimer[schluessel]
        _verfallen(warteschlange, jetzt, fenster)
        if len(warteschlange) >= grenze:
            return False
        warteschlange.append(jetzt)
        return True


def _abgelaufene_entfernen(eimer: dict, jetzt: float, fenster: int,
                           zuletzt: float | None) -> None:
    """Absender ohne Treffer im Fenster löschen - höchstens einmal je Minute."""
    zuletzt = _zuletzt_aufgeraeumt.get(id(eimer), 0.0) if zuletzt is None else zuletzt
    if jetzt - zuletzt < AUFRAEUMEN_ALLE_S:
        return
    _zuletzt_aufgeraeumt[id(eimer)] = jetzt
    for schluessel in [k for k, q in eimer.items()
                       if not q or jetzt - q[-1] > fenster]:
        del eimer[schluessel]


def login_limit(request: Request) -> None:
    """Eigenes, enges Limit für den Login - gegen das Durchprobieren."""
    if not _zaehlen(_login_treffer, client_ip(request), LOGIN_WINDOW, LOGIN_MAX):
        raise HTTPException(429, "Zu viele Anmeldeversuche. Später erneut versuchen.")


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        pfad = request.url.path
        if pfad.startswith("/api/") and not pfad.startswith(AUSGENOMMEN):
            if not _zaehlen(_treffer, client_ip(request), GLOBAL_WINDOW, GLOBAL_MAX):
                # Antwort zurückgeben statt `HTTPException` zu werfen: FastAPIs
                # Ausnahmebehandlung greift in einer Middleware nicht, der
                # Aufrufer bekam einen 500er mit Traceback im Log.
                antwort = JSONResponse({"detail": "Zu viele Anfragen."},
                                       status_code=429)
                return self._kopfzeilen(request, antwort)

        return self._kopfzeilen(request, await call_next(request))

    @staticmethod
    def _kopfzeilen(request: Request, antwort):
        antwort.headers["X-Content-Type-Options"] = "nosniff"
        antwort.headers["X-Frame-Options"] = "DENY"
        antwort.headers["Referrer-Policy"] = "same-origin"
        antwort.headers["Content-Security-Policy"] = CSP
        if request.url.scheme == "https" or request.headers.get(
                "x-forwarded-proto") == "https":
            antwort.headers["Strict-Transport-Security"] = "max-age=31536000"
        return antwort
