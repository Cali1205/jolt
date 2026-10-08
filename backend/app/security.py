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

_lock = threading.Lock()
_hit: dict[str, deque] = defaultdict(deque)
_login_hit: dict[str, deque] = defaultdict(deque)
# Fehlversuche mit einem falschen Logger-Token an /api/live/melden. Der Pfad
# ist vom allgemeinen Limit ausgenommen (er bekommt Messpunkte im Sekundentakt),
# also braucht das Durchprobieren von Tokens ein eigenes.
_report_error: dict[str, deque] = defaultdict(deque)
REPORT_ERROR_MAX = int(os.environ.get("MELDEN_FEHLER_PRO_15MIN", "30"))

# Der Live-Endpunkt bekommt im Sekundentakt Messpunkte. Ein Limit von 120
# Anfragen je Minute wäre dafür genau falsch: Es würde ausgerechnet die
# Funktion abwürgen, um die es geht.
EXEMPT = ("/api/live/",)

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

    **Von rechts lesen, nicht von links.** Ein Proxy hängt die Adresse, die er
    selbst gesehen hat, an `X-Forwarded-For` an; was davor steht, hat der
    Client geschrieben. Der erste Eintrag ist deshalb frei wählbar - wer ihn
    bei jeder Anfrage wechselt, hätte jedes Mal einen frischen Zähler und das
    Rate-Limit samt Anmeldebremse wäre wirkungslos. Gilt ist der erste Eintrag
    von rechts, der nicht selbst ein vertrauter Proxy ist.
    """
    peer = request.client.host if request.client else "unbekannt"
    if peer in TRUSTED_PROXIES:
        forwarded = request.headers.get("x-forwarded-for", "")
        entries = [e.strip() for e in forwarded.split(",") if e.strip()]
        for entry in reversed(entries):
            if entry not in TRUSTED_PROXIES:
                return entry
    return peer


def _expired(queue: deque, now_ts: float, timeframe: int) -> None:
    while queue and now_ts - queue[0] > timeframe:
        queue.popleft()


# Wie oft abgelaufene Absender aus dem Speicher genommen werden. Der Zähler
# legt je Absender-IP einen Eintrag an; ohne Aufräumen wüchse er mit jeder IP,
# die je angefragt hat - und hinter einem Proxy, der den Header nicht
# überschreibt, mit jedem erfundenen Wert.
CLEANUP_ALL_S = 60
_last_cleaned_up: dict[int, float] = {}


def _count(eimer: dict, keyname: str, timeframe: int, bound: int,
             cleanup_from: float | None = None) -> bool:
    """True, wenn die Anfrage erlaubt ist.

    `aufraeumen_ab` gibt es nur für die Prüfung: Zeitpunkt der letzten
    Aufräumrunde, damit sich die Runde ohne Warten auslösen lässt.
    """
    now_ts = time.time()
    with _lock:
        _remove_expired(eimer, now_ts, timeframe, cleanup_from)
        queue = eimer[keyname]
        _expired(queue, now_ts, timeframe)
        if len(queue) >= bound:
            return False
        queue.append(now_ts)
        return True


def _remove_expired(eimer: dict, now_ts: float, timeframe: int,
                           most_recent: float | None) -> None:
    """Absender ohne Treffer im Fenster löschen - höchstens einmal je Minute."""
    most_recent = _last_cleaned_up.get(id(eimer), 0.0) if most_recent is None else most_recent
    if now_ts - most_recent < CLEANUP_ALL_S:
        return
    _last_cleaned_up[id(eimer)] = now_ts
    for keyname in [k for k, q in eimer.items()
                       if not q or now_ts - q[-1] > timeframe]:
        del eimer[keyname]


def login_limit(request: Request) -> None:
    """Eigenes, enges Limit für den Login - gegen das Durchprobieren."""
    if not _count(_login_hit, client_ip(request), LOGIN_WINDOW, LOGIN_MAX):
        raise HTTPException(429, "Zu viele Anmeldeversuche. Später erneut versuchen.")


def report_locked(request: Request) -> bool:
    """True, wenn diese Adresse zu oft ein falsches Logger-Token geschickt hat."""
    now_ts = time.time()
    with _lock:
        _remove_expired(_report_error, now_ts, LOGIN_WINDOW, None)
        queue = _report_error.get(client_ip(request))
        if not queue:
            return False
        _expired(queue, now_ts, LOGIN_WINDOW)
        return len(queue) >= REPORT_ERROR_MAX


def count_report_error(request: Request) -> None:
    """Einen Fehlversuch vermerken - nur falsche Token, nicht jede Meldung."""
    with _lock:
        _report_error[client_ip(request)].append(time.time())


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        fs_path = request.url.path
        if fs_path.startswith("/api/") and not fs_path.startswith(EXEMPT):
            if not _count(_hit, client_ip(request), GLOBAL_WINDOW, GLOBAL_MAX):
                # Antwort zurückgeben statt `HTTPException` zu werfen: FastAPIs
                # Ausnahmebehandlung greift in einer Middleware nicht, der
                # Aufrufer bekam einen 500er mit Traceback im Log.
                response = JSONResponse({"detail": "Zu viele Anfragen."},
                                       status_code=429)
                return self._header_rows(request, response)

        return self._header_rows(request, await call_next(request))

    @staticmethod
    def _header_rows(request: Request, response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = CSP
        if request.url.scheme == "https" or request.headers.get(
                "x-forwarded-proto") == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response
