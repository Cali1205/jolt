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
    """What the client needs to know before logging in.

    This includes whether real routing is in use: a demo route looks just
    like a real one on the map, and the difference has to reach the UI -
    not just the log.
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
