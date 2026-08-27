import os

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import deps, models
from ..database import get_db
from ..laden import saeulen_import, verfuegbarkeit
from ..routing import korridor

router = APIRouter(prefix="/api/saeulen", tags=["ladesäulen"],
                   dependencies=[Depends(deps.aktuelle_sitzung)])


@router.get("/bestand")
def bestand(db: Session = Depends(get_db)):
    """Was ist überhaupt importiert? Die erste Frage, wenn ein Korridor leer
    bleibt - meist ist es nicht die Suche, sondern die leere Tabelle."""
    je_quelle = (db.query(models.Ladepunkt.quelle, func.count(models.Ladepunkt.id))
                 .group_by(models.Ladepunkt.quelle).all())
    return {"gesamt": sum(anzahl for _, anzahl in je_quelle),
            "je_quelle": {quelle: anzahl for quelle, anzahl in je_quelle}}


@router.get("/entlang/{fahrt_id}")
def entlang_fahrt(fahrt_id: int, radius_km: float = Query(8.0, gt=0, le=50),
                  min_kw: float = Query(50.0, ge=0),
                  steckertyp: str = "", db: Session = Depends(get_db)):
    """Ladepunkte im Korridor um eine geplante Fahrt.

    Sortiert nach Fortschritt entlang der Route, nicht nach Entfernung zum
    Nutzer - unterwegs ist "wie weit noch bis dahin" die einzige Ordnung,
    die zählt.
    """
    fahrt = db.get(models.Fahrt, fahrt_id)
    if not fahrt:
        raise HTTPException(404, "Fahrt nicht gefunden.")

    typ = steckertyp or fahrt.fahrzeug.steckertyp
    kandidaten = korridor.suchen(db, fahrt.geometrie or [], radius_km=radius_km,
                                 min_kw=min_kw, steckertyp=typ)

    ergebnis = []
    for kandidat in kandidaten:
        zustand = verfuegbarkeit.MELDUNGEN.zustand(kandidat.ladepunkt)
        ergebnis.append({
            **kandidat.als_dict(),
            "belegt_gemeldet": zustand.quelle == "meldung",
            "verfuegbarkeit": zustand.quelle,
            # Solange niemand weiss, was frei ist, ist die Anzahl der
            # Ladepunkte die einzige belastbare Aussage über das Risiko,
            # vor einer belegten Säule zu stehen.
            "redundanz_bonus_min": verfuegbarkeit.redundanz_bonus(
                kandidat.ladepunkt.anzahl_punkte or 1),
            # Zugangsbeschränkungen und Freitext der Quelle. Sie stehen hier
            # roh, weil sie noch nicht ausgewertet werden - aber ein Hinweis
            # wie "nur für Hotelgäste" ändert die Wahl, und ihn zu haben und
            # nicht zu zeigen wäre die schlechteste aller Möglichkeiten.
            "zugang": kandidat.ladepunkt.zugang,
            "mitgliedschaft_noetig": kandidat.ladepunkt.mitgliedschaft_noetig,
            "hinweise": kandidat.ladepunkt.hinweise or {}})

    return {"anzahl": len(ergebnis), "steckertyp": typ, "min_kw": min_kw,
            "radius_km": radius_km, "kandidaten": ergebnis}


@router.post("/{ladepunkt_id}/belegt")
def belegt_melden(ladepunkt_id: int, db: Session = Depends(get_db)):
    """"Hier ist alles voll" - die einzige Verfügbarkeitsinformation, die
    wirklich stimmt. Gilt eine halbe Stunde, danach verfällt sie."""
    if not db.get(models.Ladepunkt, ladepunkt_id):
        raise HTTPException(404, "Ladepunkt nicht gefunden.")
    verfuegbarkeit.MELDUNGEN.melden(ladepunkt_id)
    return {"ok": True, "gilt_minuten": verfuegbarkeit.MELDUNG_GUELTIG_S // 60}


@router.delete("/{ladepunkt_id}/belegt")
def belegt_zuruecknehmen(ladepunkt_id: int):
    verfuegbarkeit.MELDUNGEN.freigeben(ladepunkt_id)
    return {"ok": True}


@router.post("/import/ocm")
def import_ocm(laender: str = "DE", max_ergebnisse: int = Query(2000, le=20000),
               min_kw: float = 0.0, db: Session = Depends(get_db)):
    """Open-Charge-Map-Import anstossen.

    Die Bundesnetzagentur-Datei wird bewusst nicht hier heruntergeladen: Sie
    ist über 50 MB gross und würde die Anfrage minutenlang blockieren. Dafür
    gibt es tools/import_bnetza.py.
    """
    schluessel = os.environ.get("OCM_API_KEY", "")
    if not schluessel:
        raise HTTPException(400, "Kein OCM_API_KEY gesetzt.")
    try:
        return saeulen_import.aus_ocm(db, schluessel,
                                      laender=[l.strip() for l in laender.split(",")],
                                      max_ergebnisse=max_ergebnisse, min_kw=min_kw)
    except Exception as fehler:      # noqa: BLE001
        raise HTTPException(502, f"Import fehlgeschlagen: {fehler}") from fehler
