from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import deps, models, routing
from ..database import get_db
from ..security import login_limit

router = APIRouter(prefix="/api", tags=["zugang"])


class SignIn(BaseModel):
    password: str = ""
    device: str = ""


@router.get("/status")
def status():
    """Was der Client vor der Anmeldung wissen muss.

    Auch die Frage, ob echt geroutet wird: Eine Demo-Route sieht auf der
    Karte aus wie eine echte, und der Unterschied muss in der Oberfläche
    ankommen - nicht nur im Log.
    """
    return {"password_required": deps.password_set(),
            "demo_routing": routing.is_demo()}


@router.post("/login")
def login(records: SignIn, request: Request, db: Session = Depends(get_db)):
    login_limit(request)
    if not deps.password_set():
        return {"token": "", "hint": "Kein Passwort gesetzt - Zugang offen."}
    if not deps.examine_password(records.password):
        raise HTTPException(401, "Passwort stimmt nicht.")
    return {"token": deps.create_session(db, records.device)}


@router.post("/logout")
def logout(session: models.AuthSession | None = Depends(deps.current_session),
           db: Session = Depends(get_db)):
    if session:
        db.delete(session)
        db.commit()
    return {"ok": True}
