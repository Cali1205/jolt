from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import deps, models, routing
from ..database import get_db
from ..security import login_limit

router = APIRouter(prefix="/api", tags=["zugang"])


class Anmeldung(BaseModel):
    passwort: str = ""
    geraet: str = ""


@router.get("/status")
def status():
    """Was der Client vor der Anmeldung wissen muss.

    Auch die Frage, ob echt geroutet wird: Eine Demo-Route sieht auf der
    Karte aus wie eine echte, und der Unterschied muss in der Oberfläche
    ankommen - nicht nur im Log.
    """
    return {"passwort_noetig": deps.passwort_gesetzt(),
            "demo_routing": routing.ist_demo()}


@router.post("/login")
def login(daten: Anmeldung, request: Request, db: Session = Depends(get_db)):
    login_limit(request)
    if not deps.passwort_gesetzt():
        return {"token": "", "hinweis": "Kein Passwort gesetzt - Zugang offen."}
    if not deps.passwort_pruefen(daten.passwort):
        raise HTTPException(401, "Passwort stimmt nicht.")
    return {"token": deps.sitzung_anlegen(db, daten.geraet)}


@router.post("/logout")
def logout(sitzung: models.Sitzung | None = Depends(deps.aktuelle_sitzung),
           db: Session = Depends(get_db)):
    if sitzung:
        db.delete(sitzung)
        db.commit()
    return {"ok": True}
