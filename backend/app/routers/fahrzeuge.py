import math
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from .. import deps, models
from ..zeit import utc_iso
from ..database import get_db
from ..laden import kurven

router = APIRouter(prefix="/api/fahrzeuge", tags=["fahrzeuge"],
                   dependencies=[Depends(deps.aktuelle_sitzung)])


class Strompreis(BaseModel):
    """Ein Preis für Säulen, deren Name das Muster enthält."""
    muster: str = Field(max_length=80)
    eur_kwh: float = Field(ge=0, le=5, allow_inf_nan=False)


class FahrzeugEingabe(BaseModel):
    """Grenzen mit Luft nach oben: Sie fangen Tippfehler und kaputte Clients,
    nicht ungewöhnliche Fahrzeuge. Null oder Negatives im Fahrwiderstand ergab
    sonst Division durch null oder NaN im Verbrauchsmodell."""
    name: str = Field(min_length=1, max_length=120)
    akku_brutto_kwh: float = Field(gt=0, le=1000, allow_inf_nan=False)
    akku_netto_kwh: float = Field(gt=0, le=1000, allow_inf_nan=False)
    leermasse_kg: float = Field(default=1800.0, gt=0, le=20000, allow_inf_nan=False)
    zuladung_kg: float = Field(default=150.0, ge=0, le=10000, allow_inf_nan=False)
    c_w: float = Field(default=0.28, gt=0, le=2, allow_inf_nan=False)
    stirnflaeche_m2: float = Field(default=2.30, gt=0, le=20, allow_inf_nan=False)
    c_rr: float = Field(default=0.010, ge=0, le=0.1, allow_inf_nan=False)
    # Höchstgeschwindigkeit in km/h; None = keine Grenze im Modell.
    max_tempo_kmh: float | None = Field(default=None, ge=30, le=300)
    eta_antrieb: float = Field(default=0.88, gt=0, le=1)
    eta_rekup: float = Field(default=0.70, ge=0, le=1)
    p_neben_w: float = Field(default=350.0, ge=0, le=20000, allow_inf_nan=False)
    waermepumpe: bool = True
    reserve_soc: float = Field(default=10.0, ge=0, le=50)
    ziel_soc: float = Field(default=20.0, ge=0, le=100)
    max_ladeleistung_kw: float = Field(default=150.0, gt=0, le=1500,
                                       allow_inf_nan=False)
    steckertyp: str = Field(default="CCS", max_length=40)
    # Namen oder Namensteile, die der Ladeplan bevorzugt - kein harter Filter.
    bevorzugte_betreiber: list[Annotated[str, Field(max_length=80)]] = Field(
        default=[], max_length=50)
    # Was eine Kilowattstunde kostet. Am Fahrzeug, weil der Preis am Vertrag
    # hängt und nicht an der Säule - siehe laden/preise.py.
    strompreis_eur_kwh: float = Field(default=0.59, ge=0, le=5)
    # [{"muster": "Ionity", "eur_kwh": 0.39}, ...]
    strompreise: list[Strompreis] = Field(default=[], max_length=50)
    # [[soc, kw], ...] - leer heisst "Kurve unverändert lassen"
    ladekurve: list[list[float]] = Field(default=[], max_length=100)

    @field_validator("ladekurve")
    @classmethod
    def _kurve_gueltig(cls, kurve):
        for eintrag in kurve:
            if len(eintrag) < 2:
                continue
            soc, kw = eintrag[0], eintrag[1]
            if not (math.isfinite(soc) and math.isfinite(kw)):
                raise ValueError("Ladekurve enthält keine endliche Zahl")
            if not 0 <= soc <= 100:
                raise ValueError("Ladestand der Ladekurve liegt ausserhalb 0-100 %")
            if not 0 <= kw <= 1500:
                raise ValueError("Ladeleistung der Ladekurve liegt ausserhalb 0-1500 kW")
        return kurve


def _als_dict(fahrzeug: models.Fahrzeug) -> dict:
    return {"id": fahrzeug.id, "name": fahrzeug.name,
            "akku_brutto_kwh": fahrzeug.akku_brutto_kwh,
            "akku_netto_kwh": fahrzeug.akku_netto_kwh,
            "leermasse_kg": fahrzeug.leermasse_kg,
            "zuladung_kg": fahrzeug.zuladung_kg,
            "c_w": fahrzeug.c_w, "stirnflaeche_m2": fahrzeug.stirnflaeche_m2,
            "c_rr": fahrzeug.c_rr, "max_tempo_kmh": fahrzeug.max_tempo_kmh,
            "eta_antrieb": fahrzeug.eta_antrieb,
            "eta_rekup": fahrzeug.eta_rekup, "p_neben_w": fahrzeug.p_neben_w,
            "waermepumpe": fahrzeug.waermepumpe,
            "reserve_soc": fahrzeug.reserve_soc, "ziel_soc": fahrzeug.ziel_soc,
            "max_ladeleistung_kw": fahrzeug.max_ladeleistung_kw,
            "steckertyp": fahrzeug.steckertyp,
            "bevorzugte_betreiber": fahrzeug.bevorzugte_betreiber or [],
            "strompreis_eur_kwh": fahrzeug.strompreis_eur_kwh,
            "strompreise": fahrzeug.strompreise or [],
            "korrekturfaktor": fahrzeug.korrekturfaktor,
            # Was das Fahrzeug selbst ueber seinen Akku sagt, und wann.
            # Beides null, solange nie gemessen wurde.
            "gemessene_kapazitaet_kwh": fahrzeug.gemessene_kapazitaet_kwh,
            "kapazitaet_gemessen_am": utc_iso(fahrzeug.kapazitaet_gemessen_am),
            # Womit tatsaechlich gerechnet wird - gemessen, sonst Profil.
            "kapazitaet_kwh": fahrzeug.kapazitaet_kwh,
            # Nur ob eines eingerichtet ist, nicht welches. Das Token steht
            # genau einmal in einer Antwort - der, mit der es entsteht.
            "logger_aktiv": bool(fahrzeug.logger_token),
            "ladekurve": [[p.soc_prozent, p.kw] for p in fahrzeug.ladekurve]}


def _kurve_pruefen(paare: list[list[float]]) -> None:
    """Jeder Ladestand darf nur einmal vorkommen - die Tabelle hat dafür eine
    Unique-Constraint (fahrzeug_id, soc_prozent). Ohne diese Prüfung landet ein
    doppelter Ladestand nicht als verständliche Fehlermeldung beim Nutzer,
    sondern als nackter Datenbankfehler und HTTP 500.
    """
    gesehen: set[float] = set()
    for eintrag in paare:
        if len(eintrag) < 2:
            continue
        soc = float(eintrag[0])
        if soc in gesehen:
            raise HTTPException(
                400, f"Ladestand {soc:g} % kommt in der Ladekurve mehrfach vor - "
                     "jeder Ladestand darf nur eine Ladeleistung haben.")
        gesehen.add(soc)


def _kurve_setzen(db: Session, fahrzeug: models.Fahrzeug,
                  paare: list[list[float]]) -> None:
    for alt in list(fahrzeug.ladekurve):
        db.delete(alt)
    fahrzeug.ladekurve.clear()
    # Ohne dieses Flush schreibt SQLAlchemy im selben Flush zuerst die neuen
    # Zeilen und erst danach die DELETEs der alten - beim Bearbeiten eines
    # Fahrzeugs, das dieselben Ladestände behält (der Normalfall: nur die
    # kW-Werte ändern sich), verletzt das dann dieselbe Unique-Constraint wie
    # ein echtes Duplikat, nur unsichtbar für _kurve_pruefen(). Das Flush
    # zwingt die Löschungen zuerst in die Datenbank.
    db.flush()
    for eintrag in paare:
        if len(eintrag) < 2:
            continue
        fahrzeug.ladekurve.append(models.Ladekurvenpunkt(
            soc_prozent=float(eintrag[0]), kw=float(eintrag[1])))


@router.get("/vorlagen")
def vorlagen():
    """Startwerte, damit niemand c_w-Wert und Ladekurve von Hand raten muss.

    Ausdrücklich Näherungen und keine Herstellerangaben - sie werden über den
    Korrekturfaktor an das eigene Auto herangeführt.
    """
    return [{**v, "ladekurve": [list(p) for p in v["ladekurve"]]}
            for v in kurven.VORLAGEN]


@router.get("")
def liste(db: Session = Depends(get_db)):
    return [_als_dict(f) for f in db.query(models.Fahrzeug)
            .order_by(models.Fahrzeug.id).all()]


@router.post("")
def anlegen(eingabe: FahrzeugEingabe, db: Session = Depends(get_db)):
    if eingabe.akku_netto_kwh > eingabe.akku_brutto_kwh:
        raise HTTPException(400, "Netto-Kapazität kann nicht über brutto liegen.")
    _kurve_pruefen(eingabe.ladekurve)
    felder = eingabe.model_dump(exclude={"ladekurve"})
    fahrzeug = models.Fahrzeug(**felder)
    db.add(fahrzeug)
    db.flush()
    _kurve_setzen(db, fahrzeug, eingabe.ladekurve or
                  [list(p) for p in kurven.VORLAGEN[0]["ladekurve"]])
    db.commit()
    return _als_dict(fahrzeug)


@router.put("/{fahrzeug_id}")
def aendern(fahrzeug_id: int, eingabe: FahrzeugEingabe,
            db: Session = Depends(get_db)):
    fahrzeug = db.get(models.Fahrzeug, fahrzeug_id)
    if not fahrzeug:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")
    if eingabe.akku_netto_kwh > eingabe.akku_brutto_kwh:
        raise HTTPException(400, "Netto-Kapazität kann nicht über brutto liegen.")
    if eingabe.ladekurve:
        _kurve_pruefen(eingabe.ladekurve)
    for schluessel, wert in eingabe.model_dump(exclude={"ladekurve"}).items():
        setattr(fahrzeug, schluessel, wert)
    if eingabe.ladekurve:
        _kurve_setzen(db, fahrzeug, eingabe.ladekurve)
    db.commit()
    return _als_dict(fahrzeug)


@router.post("/{fahrzeug_id}/logger-token")
def logger_token_erneuern(fahrzeug_id: int, db: Session = Depends(get_db)):
    """Ein neues Logger-Token erzeugen - und damit das alte entwerten.

    Das Token ist der Schlüssel, mit dem ein Gerät im Auto Messpunkte melden
    darf (`POST /api/live/melden`). Es steht **nur in dieser einen Antwort**;
    danach ist es aus der Oberfläche nicht mehr abzurufen. Wer es verliert,
    erzeugt ein neues - das kostet nichts ausser dem Neueintragen im Logger,
    und es hält die Gewohnheit aufrecht, ein Geheimnis nicht in jeder
    Listenantwort mitzuschleppen.
    """
    fahrzeug = db.get(models.Fahrzeug, fahrzeug_id)
    if not fahrzeug:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")
    fahrzeug.logger_token = secrets.token_urlsafe(32)
    db.commit()
    return {"logger_token": fahrzeug.logger_token,
            "hinweis": "Dieses Token wird nur einmal angezeigt."}


@router.delete("/{fahrzeug_id}/logger-token")
def logger_token_loeschen(fahrzeug_id: int, db: Session = Depends(get_db)):
    """Den Logger abmelden. Danach wird von ihm nichts mehr angenommen."""
    fahrzeug = db.get(models.Fahrzeug, fahrzeug_id)
    if not fahrzeug:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")
    fahrzeug.logger_token = None
    db.commit()
    return {"ok": True}


@router.delete("/{fahrzeug_id}")
def loeschen(fahrzeug_id: int, db: Session = Depends(get_db)):
    fahrzeug = db.get(models.Fahrzeug, fahrzeug_id)
    if not fahrzeug:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")
    if db.query(models.Fahrt).filter_by(fahrzeug_id=fahrzeug_id).first():
        raise HTTPException(409, "Zu diesem Fahrzeug gibt es noch Fahrten.")
    db.delete(fahrzeug)
    db.commit()
    return {"ok": True}
