"""Hardening for operation behind a reverse proxy.

Contains security headers and an IP-based rate limit. Deliberately without an
external dependency: an instance with a handful of devices gets by with an
in-memory counter, and that cannot fail on its own.
"""
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

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
# Failed attempts with a wrong logger token at /api/live/melden. The path is
# exempt from the general limit (it receives measurement points every second),
# so guessing tokens needs a limit of its own.
_report_error: dict[str, deque] = defaultdict(deque)
REPORT_ERROR_MAX = int(os.environ.get("MELDEN_FEHLER_PRO_15MIN", "30"))

# The live endpoint receives measurement points every second. A limit of 120
# requests per minute would be exactly wrong for it: it would choke off the
# very function it is all about.
EXEMPT = ("/api/live/",)

CSP = ("default-src 'self'; "
       # Map tiles come from the OSM tile server, otherwise the map stays empty.
       # Both spellings: the canonical host has no subdomain and would not be
       # covered by "*.tile.openstreetmap.org" alone.
       "img-src 'self' data: blob: https://tile.openstreetmap.org "
       "https://*.tile.openstreetmap.org; "
       "style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; "
       "connect-src 'self' ws: wss:; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


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
    peer = request.client.host if request.client else "unbekannt"
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


def login_limit(request: Request) -> None:
    """Separate, tight limit for the login - against guessing."""
    if not _count(_login_hit, client_ip(request), LOGIN_WINDOW, LOGIN_MAX):
        raise HTTPException(429, "Zu viele Anmeldeversuche. Später erneut versuchen.")


def report_locked(request: Request) -> bool:
    """True if this address has sent a wrong logger token too often."""
    now = time.time()
    with _lock:
        _remove_expired(_report_error, now, LOGIN_WINDOW, None)
        queue = _report_error.get(client_ip(request))
        if not queue:
            return False
        _expired(queue, now, LOGIN_WINDOW)
        return len(queue) >= REPORT_ERROR_MAX


def count_report_error(request: Request) -> None:
    """Record a failed attempt - only wrong tokens, not every report."""
    with _lock:
        _report_error[client_ip(request)].append(time.time())


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not path.startswith(EXEMPT):
            if not _count(_hit, client_ip(request), GLOBAL_WINDOW, GLOBAL_MAX):
                # Return a response instead of raising `HTTPException`:
                # FastAPI's exception handling does not apply in a middleware,
                # the caller got a 500 with a traceback in the log.
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
