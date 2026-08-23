"""Endpunkte für die Benachrichtigungen aufs Telefon.

Der öffentliche Schlüssel darf jeder lesen - er ist dafür da, verteilt zu
werden. Das An- und Abmelden eines Geräts verlangt dagegen Zugang: Wer ein Abo
anlegen darf, bekommt die Ladepläne dieses Haushalts aufs Gerät.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps, push
from ..database import get_db

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/push", tags=["push"])


class Abo(BaseModel):
    endpoint: str = Field(min_length=8, max_length=500)
    p256dh: str = Field(min_length=8, max_length=200)
    auth: str = Field(min_length=4, max_length=100)
    geraet: str = ""


class Abmeldung(BaseModel):
    endpoint: str = Field(min_length=8, max_length=500)


@router.get("/schluessel")
def schluessel():
    """Was der Browser braucht, um ein Abo anzulegen.

    Ohne Anmeldung erreichbar: Der öffentliche Schlüssel ist kein Geheimnis,
    und die Oberfläche muss vor dem Anmelden wissen, ob sie den Knopf
    überhaupt anbieten kann.
    """
    return {"eingerichtet": push.ist_eingerichtet(),
            "schluessel": push.oeffentlicher_schluessel()}


@router.post("/abo", dependencies=[Depends(deps.aktuelle_sitzung)])
def abo_anlegen(abo: Abo, db: Session = Depends(get_db)):
    if not push.ist_eingerichtet():
        raise HTTPException(409, "Es ist kein VAPID-Schlüssel gesetzt - "
                                 "Benachrichtigungen sind aus.")
    push.abo_speichern(db, abo.endpoint, abo.p256dh, abo.auth, abo.geraet)
    return {"ok": True}


@router.delete("/abo", dependencies=[Depends(deps.aktuelle_sitzung)])
def abo_abmelden(abmeldung: Abmeldung, db: Session = Depends(get_db)):
    return {"ok": push.abo_loeschen(db, abmeldung.endpoint)}


@router.post("/probe", dependencies=[Depends(deps.aktuelle_sitzung)])
def probe(db: Session = Depends(get_db)):
    """Eine Testnachricht an alle angemeldeten Geräte.

    Der einzige Weg, das Zusammenspiel aus Schlüssel, Abo, Push-Dienst und
    Service Worker zu prüfen, ohne eine Fahrt zu machen - und der Weg, auf dem
    man merkt, dass der Schlüssel nicht zum Abo passt.
    """
    if not push.ist_eingerichtet():
        raise HTTPException(409, "Es ist kein VAPID-Schlüssel gesetzt.")
    ergebnis = push.senden(db, "jolt", "Benachrichtigungen sind eingerichtet.",
                           url="/")
    return ergebnis
