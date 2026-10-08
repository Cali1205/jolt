"""Access: one password, one token per device.

Deliberately no user management. jolt is self-hosted for one's own vehicles -
there is no second role that would be allowed to do something different, and
permission management without a second role is just code that can go wrong.

If `APP_PASSWORT` is empty, access is open. That is a deliberate option for
running in a network that nobody else can get into anyway - but it is written
to the log at startup, so that nobody leaves it that way by accident.
"""
import logging
import os
import secrets
from datetime import datetime, timedelta

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from . import models
from .database import SessionLocal, get_db
from fastapi import Depends

log = logging.getLogger("uvicorn.error")

# Expired means "not used for a long time", not "logged in long ago". A phone
# that comes along on every trip thus stays logged in - and that is exactly
# what someone relies on who does not want to type a password on the road.
SESSION_MAX_AGE = timedelta(days=180)
# At most this often is "last seen" updated.
SESSION_TOUCH = timedelta(hours=1)


def password_set() -> bool:
    return bool(os.environ.get("APP_PASSWORT", "").strip())


def examine_password(user_input: str) -> bool:
    expected = os.environ.get("APP_PASSWORT", "").strip()
    if not expected:
        return True
    # compare_digest instead of ==, so that the running time reveals nothing
    # about the password. On a home server this is paranoia with negligible
    # cost - but it is the right habit.
    # Bytes, not strings: compare_digest raises a TypeError for non-ASCII in
    # str. A password with an umlaut turned the login into a 500 - and an
    # APP_PASSWORT set up like that could never have been accepted.
    return secrets.compare_digest((user_input or "").encode("utf-8"),
                                  expected.encode("utf-8"))


def create_session(db: Session, device: str = "") -> str:
    token = secrets.token_urlsafe(32)
    db.add(models.AuthSession(token=token, device=device[:120]))
    db.commit()
    return token


def examine_session(x_token: str, db: Session) -> models.AuthSession:
    """Check the token of a login and update the session.

    A function of its own, because two paths lead in: the dependency for HTTP
    (header `X-Token`) and the WebSocket, which cannot set headers and
    therefore sends the token as its first message.
    """
    if not x_token:
        raise HTTPException(401, "Nicht angemeldet.")

    session = db.query(models.AuthSession).filter_by(token=x_token).one_or_none()
    if not session:
        raise HTTPException(401, "Sitzung unbekannt - bitte neu anmelden.")

    now_ts = datetime.utcnow()
    if now_ts - session.last_seen > SESSION_MAX_AGE:
        db.delete(session)
        db.commit()
        raise HTTPException(401, "Sitzung abgelaufen - bitte neu anmelden.")

    if now_ts - session.last_seen > SESSION_TOUCH:
        session.last_seen = now_ts
        db.commit()
    return session


def current_session(x_token: str = Header(default=""),
                     db: Session = Depends(get_db)) -> models.AuthSession | None:
    """Dependency for everything that needs access."""
    if not password_set():
        return None
    return examine_session(x_token, db)


def token_valid(x_token: str) -> bool:
    """For the WebSocket: is this token logged in? Without exception."""
    if not password_set():
        return True
    db = SessionLocal()
    try:
        examine_session(x_token or "", db)
        return True
    except HTTPException:
        return False
    finally:
        db.close()


def at_start_warn() -> None:
    if not password_set():
        log.warning("APP_PASSWORT is empty - jolt is reachable without "
                    "login. Only acceptable on your own network.")
