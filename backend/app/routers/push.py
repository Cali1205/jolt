"""Endpoints for notifications to the phone.

Anyone may read the public key - it exists to be distributed. Subscribing and
unsubscribing a device, however, requires access: whoever may create a
subscription gets this household's charging plans on their device.
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


@router.get("/key")
def public_key():
    """What the browser needs to create a subscription.

    Reachable without login: the public key is no secret, and before login
    the UI has to know whether it can offer the button at all.
    """
    return {"configured": push.actual_configured(),
            "public_key": push.pub_key()}


@router.post("/subscription", dependencies=[Depends(deps.current_session)])
def create_subscription(subscription: Subscription, db: Session = Depends(get_db)):
    if not push.actual_configured():
        raise HTTPException(409, "Es ist kein VAPID-Schlüssel gesetzt - "
                                 "Benachrichtigungen sind aus.")
    if not push.endpoint_allowed(subscription.endpoint):
        raise HTTPException(422, "Der Endpunkt muss eine https-Adresse eines "
                                 "öffentlichen Push-Dienstes sein.")
    push.save_subscription(db, subscription.endpoint, subscription.p256dh, subscription.auth, subscription.device)
    return {"ok": True}


@router.delete("/subscription", dependencies=[Depends(deps.current_session)])
def sign_out_subscription(sign_out: SignOut, db: Session = Depends(get_db)):
    return {"ok": push.delete_subscription(db, sign_out.endpoint)}


@router.post("/probe", dependencies=[Depends(deps.current_session)])
def probe(db: Session = Depends(get_db)):
    """A test message to all subscribed devices.

    The only way to check the interplay of key, subscription, push service
    and service worker without making a trip - and the way to notice that
    the key does not match the subscription.
    """
    if not push.actual_configured():
        raise HTTPException(409, "Es ist kein VAPID-Schlüssel gesetzt.")
    result = push.send(db, "jolt", "Benachrichtigungen sind eingerichtet.",
                           url="/")
    return result
