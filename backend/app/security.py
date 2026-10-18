"""Hardening for operation behind a reverse proxy.

Contains security headers and an IP-based rate limit. Deliberately without an
external dependency: an instance with a handful of devices gets by with an
in-memory counter, and that cannot fail on its own.
"""
import hashlib
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

# Only proxies from this list may set the client IP. Without the check any
# client could forge the header and bypass the rate limit.
TRUSTED_PROXIES = {p.strip() for p in
                   os.environ.get("TRUSTED_PROXIES", "").split(",") if p.strip()}

GLOBAL_WINDOW = 60
GLOBAL_MAX = int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))
LOGIN_WINDOW = 15 * 60
LOGIN_MAX = int(os.environ.get("LOGIN_LIMIT_PER_15MIN", "10"))

_lock = threading.Lock()
_hit: dict[str, deque] = defaultdict(deque)
_login_hit: dict[str, deque] = defaultdict(deque)
# The exempt measurement paths get a limit of their own: generous (a logger
# sends about one request per second per path), but not unlimited - the body
# is parsed before the token or session is checked.
_points_hit: dict[str, deque] = defaultdict(deque)
POINTS_MAX = int(os.environ.get("POINTS_LIMIT_PER_MIN", "600"))
# New WebSocket handshakes per IP and minute; a socket waits up to 10 s for
# its token, so unauthenticated sockets would otherwise pile up.
_ws_hit: dict[str, deque] = defaultdict(deque)
WS_MAX = int(os.environ.get("WS_LIMIT_PER_MIN", "30"))
# Failed attempts with a wrong logger token at /api/live/report. The path is
# exempt from the general limit (it receives measurement points every second),
# so guessing tokens needs a limit of its own.
_report_error: dict[str, deque] = defaultdict(deque)
REPORT_ERROR_MAX = int(os.environ.get("MELDEN_FEHLER_PRO_15MIN", "30"))

# The live endpoint receives measurement points every second. A limit of 120
# requests per minute would be exactly wrong for it: it would choke off the
# very function it is all about. Only these high-frequency paths are exempt;
# everything else under /api/live/ (start, end, read, simulate) costs a
# database session per call and stays under the general limit.


_EXEMPT_PATH = re.compile(r"/api/live/(report|\d+/points?)")
# Largest request body accepted on /api/. A full batch is 500 points of at
# most 24 kB raw values; anything beyond is not a measurement.
MAX_BODY_BYTES = 6_000_000


def is_exempt(path: str, method: str = "POST") -> bool:
    """Only the POST routes that receive points; GET /points is costly."""
    return method == "POST" and _EXEMPT_PATH.fullmatch(path) is not None

CSP = ("default-src 'self'; "
       # Map tiles come from the OSM tile server, otherwise the map stays empty.
       # Both spellings: the canonical host has no subdomain and would not be
       # covered by "*.tile.openstreetmap.org" alone.
       "img-src 'self' data: blob: https://tile.openstreetmap.org "
       "https://*.tile.openstreetmap.org; "
       "style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; "
       "connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


_warned_proxy = False


def client_ip(request: Request) -> str:
    """Determine the real client IP.

    With Docker port publishing the container otherwise only sees the gateway -
    all users would then share one counter.

    **Read from the right, not from the left.** A proxy appends the address it
    saw itself to `X-Forwarded-For`; whatever precedes it was written by the
    client. The first entry can therefore be chosen freely - anyone who changes
    it on every request would get a fresh counter every time, and the rate
    limit together with the login brake would be ineffective. The one that
    counts is the first entry from the right that is not itself a trusted
    proxy.
    """
    global _warned_proxy
    peer = request.client.host if request.client else "unbekannt"
    if (not TRUSTED_PROXIES and not _warned_proxy
            and request.headers.get("x-forwarded-for")):
        _warned_proxy = True
        log.warning("X-Forwarded-For received but TRUSTED_PROXIES is empty: "
                    "all clients count as %s, so the login and logger-token "
                    "locks apply to everyone at once. Set TRUSTED_PROXIES to "
                    "the proxy address.", peer)
    if peer in TRUSTED_PROXIES:
        forwarded = request.headers.get("x-forwarded-for", "")
        entries = [e.strip() for e in forwarded.split(",") if e.strip()]
        for entry in reversed(entries):
            if entry not in TRUSTED_PROXIES:
                return entry
    return peer


def _expired(queue: deque, now: float, window_s: int) -> None:
    while queue and now - queue[0] > window_s:
        queue.popleft()


# How often expired senders are removed from memory. The counter creates an
# entry per sender IP; without cleaning up it would grow with every IP that
# ever made a request - and behind a proxy that does not overwrite the header,
# with every made-up value.
CLEANUP_ALL_S = 60
_last_cleaned_up: dict[int, float] = {}


def _count(buckets: dict, key: str, window_s: int, bound: int,
             cleanup_from: float | None = None) -> bool:
    """True if the request is allowed.

    `cleanup_from` exists only for testing: time of the last cleanup round, so
    that the round can be triggered without waiting.
    """
    now = time.time()
    with _lock:
        _remove_expired(buckets, now, window_s, cleanup_from)
        queue = buckets[key]
        _expired(queue, now, window_s)
        if len(queue) >= bound:
            return False
        queue.append(now)
        return True


def _remove_expired(buckets: dict, now: float, window_s: int,
                           most_recent: float | None) -> None:
    """Delete senders without a hit in the window - at most once per minute."""
    most_recent = _last_cleaned_up.get(id(buckets), 0.0) if most_recent is None else most_recent
    if now - most_recent < CLEANUP_ALL_S:
        return
    _last_cleaned_up[id(buckets)] = now
    for key in [k for k, q in buckets.items()
                       if not q or now - q[-1] > window_s]:
        del buckets[key]


def token_hash(token: str) -> str:
    """What the database stores of a token: SHA-256 as hex (64 characters).

    The tokens are random 256-bit values, so a plain hash is enough (no salt
    or slow function needed), and the lookup stays an equality query on an
    index. A leaked dump or backup then holds no usable login or logger key.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def login_limit(request: Request) -> None:
    """Separate, tight limit for the login - against guessing.

    The attempt is **reserved** here, under the lock: a burst of parallel
    requests would otherwise all pass the check before the first failure is
    recorded. A successful login gives the slot back (`release_login`), so
    only failures count - behind a proxy without TRUSTED_PROXIES all clients
    share one counter, and successful logins must not lock everyone out.
    """
    now = time.time()
    with _lock:
        _remove_expired(_login_hit, now, LOGIN_WINDOW, None)
        queue = _login_hit[client_ip(request)]
        _expired(queue, now, LOGIN_WINDOW)
        if len(queue) >= LOGIN_MAX:
            raise HTTPException(
                429, "Zu viele Anmeldeversuche. Später erneut versuchen.")
        queue.append(now)


def release_login(request: Request) -> None:
    """The login worked (or there is no password): give the reserved slot back."""
    with _lock:
        queue = _login_hit.get(client_ip(request))
        if queue:
            queue.pop()


def report_locked(request: Request) -> bool:
    """True if this address has sent a wrong logger token too often.

    Otherwise the attempt is reserved (see `login_limit`); a valid token
    gives it back with `count_report_ok`.
    """
    now = time.time()
    with _lock:
        _remove_expired(_report_error, now, LOGIN_WINDOW, None)
        queue = _report_error[client_ip(request)]
        _expired(queue, now, LOGIN_WINDOW)
        if len(queue) >= REPORT_ERROR_MAX:
            return True
        queue.append(now)
        return False


def count_report_ok(request: Request) -> None:
    """The token was valid: give the reserved slot back."""
    with _lock:
        queue = _report_error.get(client_ip(request))
        if queue:
            queue.pop()


def websocket_allowed(websocket) -> bool:
    """False if this IP opened too many WebSockets in the last minute."""
    return _count(_ws_hit, client_ip(websocket), 60, WS_MAX)


def count_report_error(request: Request) -> None:
    """Kept for callers: a wrong token keeps the slot reserved by `report_locked`."""


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            refusal = self._refuse_body(request)
            if refusal is not None:
                return self._header_rows(request, refusal)
        if path.startswith("/api/") and is_exempt(path, request.method):
            if not _count(_points_hit, client_ip(request), GLOBAL_WINDOW, POINTS_MAX):
                return self._header_rows(request, JSONResponse(
                    {"detail": "Zu viele Anfragen."}, status_code=429))
        if path.startswith("/api/") and not is_exempt(path, request.method):
            if not _count(_hit, client_ip(request), GLOBAL_WINDOW, GLOBAL_MAX):
                # Return a response instead of raising `HTTPException`:
                # FastAPI's exception handling does not apply in a middleware,
                # the caller got a 500 with a traceback in the log.
                response = JSONResponse({"detail": "Zu viele Anfragen."},
                                       status_code=429)
                return self._header_rows(request, response)

        return self._header_rows(request, await call_next(request))

    @staticmethod
    def _refuse_body(request: Request):
        """The size limit needs a declared length: without one (chunked) or with
        an unreadable one the limit could not be enforced before parsing."""
        declared = request.headers.get("content-length")
        if declared is None:
            if (request.method in ("POST", "PUT", "PATCH")
                    and "chunked" in request.headers.get("transfer-encoding", "").lower()):
                return JSONResponse({"detail": "Content-Length fehlt."}, status_code=411)
            return None
        try:
            size = int(declared)
        except ValueError:
            return JSONResponse({"detail": "Content-Length ungültig."}, status_code=400)
        if size > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Anfrage zu groß."}, status_code=413)
        return None

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
