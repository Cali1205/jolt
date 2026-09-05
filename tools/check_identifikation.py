#!/usr/bin/env python3
"""Prüft die Parameteridentifikation an einer Fahrt, deren Wahrheit feststeht.

Der Trick dieses Skripts: Es erzeugt eine synthetische Fahrt, deren
Verbrauch mit **bekannten** Parametern gerechnet wurde, und verlangt, dass
die Ausgleichsrechnung genau diese Parameter zurückgibt. Bei einer echten
Fahrt lässt sich das nie prüfen - dort ist jede Abweichung womöglich das
Auto und nicht der Schätzer.

Dass das nötig ist, hat die Entwicklung gezeigt: Zwei Vorzeichen- und
Normierungsfehler ergaben Parametersätze, die plausibel aussahen und falsch
waren (η_rekup = 1,39 - bergab käme mehr zurück als hineingesteckt wurde).
Beide fielen erst an einer Fahrt auf, deren Antwort man vorher kannte.

    ./tools/check_identifikation.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

# Reine Rechnung - keine Datenbank, kein Netz.
anwendung_bereitstellen("identifikation", datenbank=False)

from app.energie import identifikation as ident  # noqa: E402
from app.energie.identifikation import Messpunkt  # noqa: E402

pruefe = Pruefung()

# Die Wahrheit, die zurückkommen muss.
WAHR = {"f_roll": 200.0, "cw_a": 0.65, "auf": 1.15, "ab": 0.70,
        "p_neben": 800.0}
MASSE = 2000.0
TEMP = 15.0


def fahrt_bauen(tempi, steigungen, punkte_je_abschnitt=14, takt_s=12.0):
    """Eine Fahrt aus Abschnitten mit je festem Tempo und fester Steigung.

    Die Höhe wird *linear* gesetzt und nicht aus verrauschten Kartendaten -
    hier soll der Schätzer geprüft werden, nicht die Datenqualität.
    """
    punkte, t, strecke_m, hoehe, entladen, geladen = [], 0.0, 0.0, 300.0, 0.0, 0.0
    lat0, lon0 = 48.0, 9.0
    # Ein Grad Breite sind rund 111,32 km - damit wird aus Metern eine
    # Koordinate, die haversine_m wieder in dieselben Meter zurückrechnet.
    grad_je_m = 1.0 / 111320.0

    for v_kmh, steig in zip(tempi, steigungen):
        v = v_kmh / 3.6
        for _ in range(punkte_je_abschnitt):
            ds = v * takt_s
            dh = ds * steig / 100.0
            rho = ident.luftdichte(TEMP, hoehe + dh / 2)
            kraft = (WAHR["f_roll"] + WAHR["cw_a"] * 0.5 * rho * v * v
                     + WAHR["p_neben"] / v)
            kraft += (WAHR["auf"] if steig >= 0 else WAHR["ab"]) \
                * MASSE * ident.G * (steig / 100.0)
            wh = kraft * ds / 3600.0
            # Auf zwei Zähler aufteilen, wie es das Fahrzeug meldet:
            # Negativbedarf ist Rekuperation und wächst auf `geladen`.
            if wh >= 0:
                entladen += wh
            else:
                geladen += -wh
            strecke_m += ds
            hoehe += dh
            t += takt_s
            punkte.append(Messpunkt(
                zeit_s=t, lat=lat0 + strecke_m * grad_je_m, lon=lon0,
                hoehe_m=hoehe, entladen_wh=entladen, geladen_wh=geladen))
    return punkte


pruefe.abschnitt("Fenster bilden")

# Genug Streuung in Tempo *und* Steigung, sonst sind die Parameter nicht
# trennbar - genau das behauptet die Konditionsprüfung, und hier wird sie
# beim Wort genommen.
tempi = [130, 90, 60, 110, 75, 130, 100, 45, 120, 85, 65, 135, 95, 55,
         125, 105, 70, 115, 80, 140, 50, 100, 90, 60]
steigungen = [0, 2, -2, 1, -1, 0, 3, -3, 0, 1.5, -1.5, 0.5, 2.5, -2.5,
              -0.5, 1, -1, 0, 2, -2, 3, -3, 0.5, -0.5]
punkte = fahrt_bauen(tempi, steigungen)

# Glättung aus: Die synthetische Höhe ist exakt, und Glätten würde die
# Anstiege verkleinern - genau der Effekt, vor dem das Modul warnt.
fenster = ident.fenster_bilden(punkte, MASSE, temp_c=TEMP, glaettung_m=0.0)
pruefe(len(fenster) >= 20, f"aus {len(punkte)} Punkten entstehen genug Fenster",
       f"nur {len(fenster)}")
pruefe(all(f.strecke_m >= ident.FENSTER_M for f in fenster),
       "jedes Fenster reisst die Mindestlänge")
pruefe(all(f.dauer_s >= ident.FENSTER_S for f in fenster),
       "jedes Fenster reisst die Mindestdauer")

# Die Fenster müssen den Grossteil der Fahrt abdecken. Was an den Rändern
# liegenbleibt, ist der Rest, der die Mindestlänge nicht mehr füllt - viel
# mehr als ein Fenster je Abschnitt darf es nicht sein.
gesamt_m = ident.haversine_m(punkte[0].lat, punkte[0].lon,
                             punkte[-1].lat, punkte[-1].lon)
abgedeckt = sum(f.strecke_m for f in fenster)
pruefe(abgedeckt > 0.8 * gesamt_m,
       f"die Fenster decken {100*abgedeckt/gesamt_m:.0f} % der Strecke ab",
       f"nur {100*abgedeckt/gesamt_m:.0f} %")

pruefe.abschnitt("Parameter zurückgewinnen")

erg = ident.identifizieren(fenster, MASSE, lam=0.0)
pruefe(erg.r2 > 0.999, f"R² nahe 1 bei rauschfreien Daten (R²={erg.r2:.4f})",
       f"R²={erg.r2:.4f}")
pruefe(erg.kondition < 100,
       f"Kondition brauchbar ({erg.kondition:.0f})", f"{erg.kondition:.0f}")

for name, wahr, ist in (
        ("F_roll", WAHR["f_roll"], erg.f_roll_n),
        ("c_w·A", WAHR["cw_a"], erg.cw_a_m2),
        ("1/eta", WAHR["auf"], erg.auf_faktor),
        ("eta_rek", WAHR["ab"], erg.eta_rekup),
        ("P_neben", WAHR["p_neben"], erg.p_neben_w)):
    rel = abs(ist - wahr) / abs(wahr)
    pruefe(rel < 0.05, f"{name} auf 5 % genau zurückgewonnen "
                       f"({ist:.3f} gegen {wahr:.3f})",
           f"{ist:.3f} statt {wahr:.3f}, {100*rel:.1f} % daneben")

pruefe.abschnitt("Die Verbrauchskurve stimmt mit der Wahrheit überein")

for v, steig in ((130, 0.0), (100, 0.0), (80, 2.0), (110, -1.0)):
    vv = v / 3.6
    rho = ident.luftdichte(TEMP, 400.0)
    soll = (WAHR["f_roll"] + WAHR["cw_a"] * 0.5 * rho * vv * vv
            + WAHR["p_neben"] / vv
            + (WAHR["auf"] if steig >= 0 else WAHR["ab"])
            * MASSE * ident.G * steig / 100.0) / 3.6
    ist = ident.verbrauch_wh_km(erg, v, steig, temp_c=TEMP)
    pruefe(abs(ist - soll) < 0.03 * abs(soll),
           f"{v} km/h bei {steig:+.0f} %: {ist:.0f} Wh/km wie gerechnet",
           f"{ist:.1f} statt {soll:.1f}")

pruefe.abschnitt("Physikalisch Unmögliches wird gemeldet")

# Dieselbe Fahrt, aber mit zu niedrig angesetzter Masse ausgewertet. Genau
# der Fall, der bei der echten Fahrt vom 4.9. auftrat: η_rekup rutscht über
# 1, weil der Steigungsterm zu klein angesetzt ist.
zu_leicht = ident.identifizieren(fenster, MASSE * 0.6, lam=0.0)
pruefe(zu_leicht.eta_rekup > 1.0,
       "zu leicht angesetzte Masse treibt η_rekup über 1",
       f"η_rekup={zu_leicht.eta_rekup:.2f}")
pruefe(any("η_rekup" in w for w in zu_leicht.warnungen),
       "und das wird als Warnung gemeldet",
       f"Warnungen: {zu_leicht.warnungen}")

# Eine Fahrt ohne Steigungsstreuung: Die Summe muss stimmen, die Aufteilung
# darf es nicht behaupten.
eben = ident.fenster_bilden(
    fahrt_bauen([120, 118, 122, 119, 121] * 5, [0.0] * 25),
    MASSE, temp_c=TEMP, glaettung_m=0.0)
erg_eben = ident.identifizieren(eben, MASSE)
pruefe(bool(erg_eben.warnungen),
       "eine Fahrt ohne Streuung meldet, dass sie nichts trennen kann",
       "keine Warnung trotz fehlender Streuung")

pruefe.abschnitt("Lücken und Ladepausen fallen heraus")

mit_luecke = list(punkte)
for p in mit_luecke[120:]:
    p.zeit_s += 3600.0          # eine Stunde Pause mitten hinein
f_luecke = ident.fenster_bilden(mit_luecke, MASSE, temp_c=TEMP, glaettung_m=0.0)
pruefe(all(not (f.von_s < punkte[120].zeit_s < f.bis_s) for f in f_luecke),
       "kein Fenster spannt über die Lücke hinweg")

sys.exit(pruefe.bilanz(
    "Nicht geprüft: echte Höhendaten, echte GPS-Streuung und die Frage, ob "
    "die Fenstergrössen für eine reale Fahrt gut gewählt sind."))
