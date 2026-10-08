"""Zugang: ein Passwort, ein Token je Gerät.

Bewusst keine Nutzerverwaltung. jolt ist selbstgehostet für die eigenen
Fahrzeuge - es gibt keine zweite Rolle, die etwas anderes dürfte, und eine
Rechteverwaltung ohne zweite Rolle ist nur Code, der schiefgehen kann.

Ist `APP_PASSWORT` leer, ist der Zugang offen. Das ist eine bewusste Option
für den Betrieb in einem Netz, in das ohnehin niemand sonst hineinkommt -
aber es steht beim Start im Log, damit es niemand versehentlich so lässt.
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

# Abgelaufen heisst "lange nicht benutzt", nicht "lange her angemeldet". Ein
# Telefon, das bei jeder Fahrt dabei ist, bleibt damit angemeldet - und genau
# darauf verlässt sich, wer unterwegs nicht erst ein Passwort tippen will.
SESSION_MAX_AGE = timedelta(days=180)
# So oft höchstens wird "zuletzt gesehen" fortgeschrieben.
SESSION_TOUCH = timedelta(hours=1)


def password_set() -> bool:
    return bool(os.environ.get("APP_PASSWORT", "").strip())


def examine_password(user_input: str) -> bool:
    expected = os.environ.get("APP_PASSWORT", "").strip()
    if not expected:
        return True
    # compare_digest statt ==, damit die Laufzeit nichts über das Passwort
    # verrät. Bei einem Heimserver ist das Paranoia mit vernachlässigbaren
    # Kosten - aber es ist die richtige Gewohnheit.
    # Bytes, nicht Strings: compare_digest wirft bei Nicht-ASCII in str einen
    # TypeError. Ein Passwort mit Umlaut machte den Login zum 500er - und ein
    # so eingerichtetes APP_PASSWORT hätte nie angenommen werden können.
    return secrets.compare_digest((user_input or "").encode("utf-8"),
                                  expected.encode("utf-8"))


def create_session(db: Session, device: str = "") -> str:
    token = secrets.token_urlsafe(32)
    db.add(models.AuthSession(token=token, device=device[:120]))
    db.commit()
    return token


def examine_session(x_token: str, db: Session) -> models.AuthSession:
    """Den Token einer Anmeldung prüfen und die Sitzung fortschreiben.

    Eigene Funktion, weil zwei Wege hineinführen: die Dependency für HTTP
    (Header `X-Token`) und der WebSocket, der keine Header setzen kann und
    den Token deshalb als erste Nachricht schickt.
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
    """Dependency für alles, was Zugang braucht."""
    if not password_set():
        return None
    return examine_session(x_token, db)


def token_valid(x_token: str) -> bool:
    """Für den WebSocket: ist dieser Token angemeldet? Ohne Ausnahme."""
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
        log.warning("APP_PASSWORT ist leer - jolt ist ohne Anmeldung "
                    "erreichbar. Nur im eigenen Netz vertretbar.")
