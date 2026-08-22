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
from .database import get_db
from fastapi import Depends

log = logging.getLogger("uvicorn.error")

# Abgelaufen heisst "lange nicht benutzt", nicht "lange her angemeldet". Ein
# Telefon, das bei jeder Fahrt dabei ist, bleibt damit angemeldet - und genau
# darauf verlässt sich, wer unterwegs nicht erst ein Passwort tippen will.
SITZUNG_MAX_ALTER = timedelta(days=180)
# So oft höchstens wird "zuletzt gesehen" fortgeschrieben.
SITZUNG_BERUEHREN = timedelta(hours=1)


def passwort_gesetzt() -> bool:
    return bool(os.environ.get("APP_PASSWORT", "").strip())


def passwort_pruefen(eingabe: str) -> bool:
    erwartet = os.environ.get("APP_PASSWORT", "").strip()
    if not erwartet:
        return True
    # compare_digest statt ==, damit die Laufzeit nichts über das Passwort
    # verrät. Bei einem Heimserver ist das Paranoia mit vernachlässigbaren
    # Kosten - aber es ist die richtige Gewohnheit.
    return secrets.compare_digest(eingabe or "", erwartet)


def sitzung_anlegen(db: Session, geraet: str = "") -> str:
    token = secrets.token_urlsafe(32)
    db.add(models.Sitzung(token=token, geraet=geraet[:120]))
    db.commit()
    return token


def aktuelle_sitzung(x_token: str = Header(default=""),
                     db: Session = Depends(get_db)) -> models.Sitzung | None:
    """Dependency für alles, was Zugang braucht."""
    if not passwort_gesetzt():
        return None

    if not x_token:
        raise HTTPException(401, "Nicht angemeldet.")

    sitzung = db.query(models.Sitzung).filter_by(token=x_token).one_or_none()
    if not sitzung:
        raise HTTPException(401, "Sitzung unbekannt - bitte neu anmelden.")

    jetzt = datetime.utcnow()
    if jetzt - sitzung.zuletzt_gesehen > SITZUNG_MAX_ALTER:
        db.delete(sitzung)
        db.commit()
        raise HTTPException(401, "Sitzung abgelaufen - bitte neu anmelden.")

    if jetzt - sitzung.zuletzt_gesehen > SITZUNG_BERUEHREN:
        sitzung.zuletzt_gesehen = jetzt
        db.commit()
    return sitzung


def beim_start_warnen() -> None:
    if not passwort_gesetzt():
        log.warning("APP_PASSWORT ist leer - jolt ist ohne Anmeldung "
                    "erreichbar. Nur im eigenen Netz vertretbar.")
