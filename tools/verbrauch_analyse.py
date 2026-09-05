#!/usr/bin/env python3
"""Den Verbrauch einer gefahrenen Sitzung nach Tempo und Steigung auswerten.

    docker exec jolt-app python3 tools/verbrauch_analyse.py [sitzung_id]

Ohne Angabe wird die jüngste Sitzung genommen.

**Wozu das neben `kalibrierung.py` steht.** Die Kalibrierung lernt einen
Korrekturfaktor - eine Zahl, die die Verbrauchskurve verschiebt. Dieses
Werkzeug beantwortet die andere Frage: *Stimmt die Form der Kurve?* Ein
Modell, das bei 90 km/h passt und bei 130 um ein Viertel danebenliegt, lässt
sich durch keinen Faktor retten, und genau daran hängt jeder Ladeplan auf der
Autobahn.

Es schreibt nichts. Was es findet, ist ein Befund und keine Einstellung -
ob daraus ein geänderter c_w-Wert im Fahrzeugprofil wird, entscheidet ein
Mensch, der die Warnungen gelesen hat.

**Die Höhe kommt aus Kartendaten**, nicht aus dem GPS. Dessen Höhenangabe
streut um zehn bis zwanzig Meter; wer solche Differenzen aufsummiert, findet
für eine Fahrt durch die Ebene mehrere hundert Höhenmeter und schreibt sie
dem Steigungsterm zu. Dieselbe Begründung wie in live/aufzeichnung.py.
"""
import os
import sys

# Suchpfad wie in pruefen.py - aber *ohne* dessen Wegwerf-Datenbank und ohne
# das Löschen von ORS_API_KEY: Dieses Werkzeug soll die echte Datenbank
# lesen und echte Höhen holen.
_hier = os.path.dirname(os.path.abspath(__file__))
for _kandidat in (os.path.join(_hier, "..", "backend"), os.path.join(_hier, "..")):
    if os.path.isdir(os.path.join(_kandidat, "app")):
        if _kandidat not in sys.path:
            sys.path.insert(0, _kandidat)
        break

from sqlalchemy import select                        # noqa: E402

from app import models, routing                      # noqa: E402
from app.database import SessionLocal                # noqa: E402
from app.energie import identifikation as ident      # noqa: E402
from app.energie.identifikation import Messpunkt     # noqa: E402
from app.energie.modell import Fahrzeugwerte, G      # noqa: E402

TEMP_C = 15.0


def hoehen_holen(punkte):
    """Geländehöhe je Messpunkt - Karte zuerst, GPS als Rückfall."""
    geometrie = [[p.lon, p.lat] for p in punkte]
    try:
        mit_hoehe = routing.provider().hoehen(geometrie)
    except Exception as fehler:                      # noqa: BLE001
        print(f"  Höhenabfrage fehlgeschlagen: {fehler}")
        mit_hoehe = None
    if mit_hoehe and len(mit_hoehe) == len(punkte):
        print(f"  Höhe aus Kartendaten für {len(punkte)} Punkte.")
        return [float(p[2]) for p in mit_hoehe]

    gps = [(p.rohwerte or {}).get("hoehe_m") for p in punkte]
    if any(h is not None for h in gps):
        print("  ACHTUNG: keine Kartenhöhe - GPS-Höhe als Rückfall. Der "
              "Steigungsterm ist damit nur grob.")
        letzte = 0.0
        aus = []
        for h in gps:
            letzte = float(h) if h is not None else letzte
            aus.append(letzte)
        return aus
    print("  ACHTUNG: gar keine Höhendaten - es wird eben gerechnet. Alles, "
          "was die Steigung gekostet hat, landet in den übrigen Parametern.")
    return [0.0] * len(punkte)


def sitzung_laden(db, sitzung_id):
    if sitzung_id:
        s = db.get(models.LiveSitzung, sitzung_id)
        if not s:
            raise SystemExit(f"Sitzung {sitzung_id} gibt es nicht.")
        return s
    s = db.execute(select(models.LiveSitzung)
                   .order_by(models.LiveSitzung.id.desc())
                   .limit(1)).scalars().first()
    if not s:
        raise SystemExit("Keine Sitzung in der Datenbank.")
    return s


def messpunkte(punkte, hoehen):
    """LivePunkt -> Messpunkt. Die Zähler stehen in `rohwerte` in kWh."""
    t0 = punkte[0].zeit
    aus = []
    for p, h in zip(punkte, hoehen):
        roh = p.rohwerte if isinstance(p.rohwerte, dict) else {}
        ent, gel = roh.get("entladen_kwh"), roh.get("geladen_kwh")
        aus.append(Messpunkt(
            zeit_s=(p.zeit - t0).total_seconds(),
            lat=p.lat, lon=p.lon, hoehe_m=h,
            entladen_wh=None if ent is None else float(ent) * 1000.0,
            geladen_wh=None if gel is None else float(gel) * 1000.0))
    return aus


def modell_wh_km(werte, tempo_kmh, steigung_pct=0.0):
    """Was `modell.py` heute für dieselbe Lage vorhersagt."""
    from app.energie.modell import hvac_leistung_w, luftdichte
    v = tempo_kmh / 3.6
    rho = luftdichte(TEMP_C, 400.0)
    kraft = (werte.c_rr * werte.masse_kg * G
             + 0.5 * rho * werte.c_w * werte.stirnflaeche_m2 * v * v)
    kraft /= werte.eta_antrieb
    neigung = steigung_pct / 100.0
    steig = werte.masse_kg * G * neigung
    kraft += steig / werte.eta_antrieb if neigung >= 0 else steig * werte.eta_rekup
    kraft += (werte.p_neben_w
              + hvac_leistung_w(TEMP_C, werte.waermepumpe)) / v
    return kraft / 3.6


def main():
    sitzung_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    db = SessionLocal()
    try:
        sitzung = sitzung_laden(db, sitzung_id)
        werte = Fahrzeugwerte.aus_fahrt(sitzung.fahrt)
        punkte = [p for p in sitzung.punkte if p.zeit and p.lat and p.lon]
        print(f"=== Sitzung {sitzung.id} (Fahrt {sitzung.fahrt_id}), "
              f"{len(punkte)} Messpunkte ===")
        print(f"  Fahrzeug: {sitzung.fahrt.fahrzeug.name}, "
              f"{werte.masse_kg:.0f} kg, c_w·A = "
              f"{werte.c_w * werte.stirnflaeche_m2:.3f} m²")
        if len(punkte) < 50:
            raise SystemExit("  Zu wenige Messpunkte für eine Auswertung.")

        hoehen = hoehen_holen(punkte)
        mp = messpunkte(punkte, hoehen)
        fenster = ident.fenster_bilden(mp, werte.masse_kg, temp_c=TEMP_C)
        if len(fenster) < 10:
            raise SystemExit(
                f"  Nur {len(fenster)} auswertbare Fenster - zu wenig. "
                "Meist liegt es an Lücken oder fehlenden Fahrzeugdaten.")

        km = sum(f.strecke_m for f in fenster) / 1000.0
        print(f"\n=== {len(fenster)} Fenster, {km:.0f} km auswertbar "
              f"({100*km/max(0.001, _gefahren_km(punkte)):.0f} % der Fahrt) ===")

        erg = ident.identifizieren(fenster, werte.masse_kg)
        print(f"  R² = {erg.r2:.3f}   Konditionszahl {erg.kondition:.0f}\n")
        # Verglichen werden *wirksame* Werte, also mit eingerechnetem
        # Wirkungsgrad - anders ist die Messung an der Batterie nicht zu
        # deuten. Deshalb steht in der Modellspalte überall das /eta.
        print(f"  {'Anteil (wirksam)':20s} {'gemessen':>20s} {'im Modell':>11s}")
        for titel, wert, fehler, vorgabe in (
                ("Rollwiderstand [N]", erg.f_roll_n, erg.fehler["f_roll"],
                 werte.c_rr * werte.masse_kg * G / werte.eta_antrieb),
                ("Luft c_w·A/eta [m²]", erg.cw_a_m2, erg.fehler["cw_a"],
                 werte.c_w * werte.stirnflaeche_m2 / werte.eta_antrieb),
                ("Bergauf 1/eta", erg.auf_faktor, erg.fehler["auf"],
                 1 / werte.eta_antrieb),
                ("Bergab eta_rekup", erg.eta_rekup, erg.fehler["ab"],
                 werte.eta_rekup),
                ("Nebenverbraucher [W]", erg.p_neben_w, erg.fehler["p_neben"],
                 werte.p_neben_w),
                ("Beschleunigen", erg.beschl_anteil, erg.fehler["beschl"],
                 0.0)):
            print(f"  {titel:20s} {wert:11.3f} ±{fehler:7.3f} {vorgabe:11.3f}")

        if erg.warnungen:
            print("\n  Was diese Fahrt *nicht* hergibt:")
            for w in erg.warnungen:
                print(f"    - {w}")

        print("\n=== Verbrauch nach Tempo (eben) ===")
        print(f"  {'Tempo':>8s} {'gemessen':>10s} {'Modell':>9s} {'Abw.':>7s}")
        for v in (60, 80, 100, 110, 120, 130):
            a = ident.verbrauch_wh_km(erg, v, temp_c=TEMP_C)
            b = modell_wh_km(werte, v)
            print(f"  {v:5d} km/h {a:8.0f} {b:9.0f} {100*(a/b-1):+6.0f} %")

        print("\n=== Verbrauch nach Steigung (bei 110 km/h) ===")
        print(f"  {'Steigung':>9s} {'gemessen':>10s} {'Modell':>9s}")
        for s in (-3, -2, -1, 0, 1, 2, 3):
            print(f"  {s:+7d} % {ident.verbrauch_wh_km(erg, 110, s, TEMP_C):8.0f} "
                  f"{modell_wh_km(werte, 110, s):9.0f}")

        print("\n=== Was Langsamerfahren bringt (eben, gemessene Kurve) ===")
        basis = ident.verbrauch_wh_km(erg, 130, temp_c=TEMP_C)
        for ziel in (120, 110, 100, 90):
            w = ident.verbrauch_wh_km(erg, ziel, temp_c=TEMP_C)
            zeit = (100.0 / ziel - 100.0 / 130) * 60.0
            print(f"  130 -> {ziel:3d} km/h: {100*(w/basis-1):+5.0f} % Verbrauch, "
                  f"{100*(basis/w-1):+5.0f} % Reichweite, "
                  f"{zeit:+4.0f} min je 100 km")

        print("\n=== Gemessene Fenster nach Tempoband ===")
        print("  (roh, also mit der Steigung darin - der Vergleich zur "
              "bereinigten\n   Kurve oben zeigt, wie stark das Profil "
              "hineinspielt)")
        bins: dict = {}
        for f in fenster:
            bins.setdefault(int(f.tempo_kmh // 20) * 20, []).append(f)
        for lo in sorted(bins):
            g = bins[lo]
            strecke = sum(f.strecke_m for f in g)
            print(f"  {lo:3d}-{lo+19:3d} km/h  {len(g):3d} Fenster "
                  f"{strecke/1000:6.1f} km  "
                  f"{sum(f.energie_wh for f in g)/(strecke/1000):6.0f} Wh/km  "
                  f"(Schnittsteigung {100*sum(f.netto_hoehe_m for f in g)/strecke:+.2f} %)")
    finally:
        db.close()


def _gefahren_km(punkte):
    from app.geo import haversine_m
    return sum(haversine_m(a.lat, a.lon, b.lat, b.lon)
               for a, b in zip(punkte, punkte[1:])) / 1000.0


if __name__ == "__main__":
    main()
