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


class Subscription(BaseModel):
    endpoint: str = Field(min_length=8, max_length=500)
    p256dh: str = Field(min_length=8, max_length=200)
    auth: str = Field(min_length=4, max_length=100)
    device: str = ""


class SignOut(BaseModel):
    endpoint: str = Field(min_length=8, max_length=500)


@router.get("/schluessel")
def keyname():
    """Was der Browser braucht, um ein Abo anzulegen.

    Ohne Anmeldung erreichbar: Der öffentliche Schlüssel ist kein Geheimnis,
    und die Oberfläche muss vor dem Anmelden wissen, ob sie den Knopf
    überhaupt anbieten kann.
    """
    return {"configured": push.actual_configured(),
            "keyname": push.pub_key()}


@router.post("/abo", dependencies=[Depends(deps.current_session)])
def create_subscription(subscription: Subscription, db: Session = Depends(get_db)):
    if not push.actual_configured():
        raise HTTPException(409, "Es ist kein VAPID-Schlüssel gesetzt - "
                                 "Benachrichtigungen sind aus.")
    if not push.endpoint_allowed(subscription.endpoint):
        raise HTTPException(422, "Der Endpunkt muss eine https-Adresse eines "
                                 "öffentlichen Push-Dienstes sein.")
    push.save_subscription(db, subscription.endpoint, subscription.p256dh, subscription.auth, subscription.device)
    return {"ok": True}


@router.delete("/abo", dependencies=[Depends(deps.current_session)])
def sign_out_subscription(sign_out: SignOut, db: Session = Depends(get_db)):
    return {"ok": push.delete_subscription(db, sign_out.endpoint)}


@router.post("/probe", dependencies=[Depends(deps.current_session)])
def probe(db: Session = Depends(get_db)):
    """Eine Testnachricht an alle angemeldeten Geräte.

    Der einzige Weg, das Zusammenspiel aus Schlüssel, Abo, Push-Dienst und
    Service Worker zu prüfen, ohne eine Fahrt zu machen - und der Weg, auf dem
    man merkt, dass der Schlüssel nicht zum Abo passt.
    """
    if not push.actual_configured():
        raise HTTPException(409, "Es ist kein VAPID-Schlüssel gesetzt.")
    result = push.send(db, "jolt", "Benachrichtigungen sind eingerichtet.",
                           url="/")
    return result
