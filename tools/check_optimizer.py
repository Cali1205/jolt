#!/usr/bin/env python3
"""Prüft den Ladestopp-Optimierer an Fällen, deren Ergebnis man vorher kennt.

Kein Testframework, keine Netzverbindung, keine Datenbank - `python3
tools/check_optimizer.py` genügt.

Der Prüfstein ist bewusst nicht die Selbstauskunft des Optimierers, sondern ein
zweiter, unabhängiger Nachrechner: Er fährt den fertigen Plan Kilometer für
Kilometer ab und schaut, ob der Ladestand irgendwo unter die Reserve fällt.
Ein Planer, der sich selbst bestätigt, prüft nichts.

Geprüft wird ausserdem gegen die beiden Fälle, an denen ein gieriger Planer
laut Konzept systematisch scheitert - eine lange Lücke ohne Schnelllader und
die Wahl zwischen einer nahen schwachen und einer weiteren starken Säule.

    ./tools/check_optimizer.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("optim", db_name=False)

from app.energy.model import VehicleValues  # noqa: E402
from app.charging import optimizer  # noqa: E402

verify = Check()

# Eine Kurve mit deutlichem Knick: volle Leistung bis 40 %, danach fällt sie
# steil ab. Genau daran muss sich zeigen, ob der Optimierer die Ladehübe in
# den steilen Teil legt.
CURVE = [(0, 110), (10, 120), (30, 120), (50, 90), (70, 60), (80, 45),
         (90, 28), (100, 8)]


def section(title: str) -> None:
    print(f"\n{title}")


# ---------------------------------------------------------------------------
# Werkzeug
# ---------------------------------------------------------------------------

def profile_build(km_total: float, kwh_per_km: float = 0.18,
                 speed_kmh: float = 120.0, kwh_at=None):
    """Ein synthetisches Streckenprofil, ein Stützpunkt je Kilometer.

    `kwh_at` erlaubt einen beliebigen Verlauf der kumulierten Energie - so
    lässt sich ein Pass bauen, bei dem die Bilanz am Ende harmlos aussieht und
    unterwegs trotzdem nichts mehr im Akku ist.
    """
    km = [float(i) for i in range(int(km_total) + 1)]
    kwh = [kwh_at(k) for k in km] if kwh_at else [k * kwh_per_km for k in km]
    return optimizer.RouteProfile(
        km=km, kwh=kwh, mins=[k / speed_kmh * 60.0 for k in km])


def chargers(kms, max_kw=150.0, detour=5.0, point_count=4, from_id=1):
    return [optimizer.ChargeOption(id=from_id + i, km_on_route=float(k),
                                  detour_minutes=detour, max_kw=max_kw,
                                  point_count=point_count,
                                  name=f"Lader km {k:.0f}")
            for i, k in enumerate(kms)]


def recompute(plan, profile, fz, start_soc: float) -> dict:
    """Den fertigen Plan unabhängig nachfahren.

    Der eigentliche Test: Der Optimierer behauptet Ankunfts- und Abfahrtswerte;
    hier wird nachgesehen, ob sie zum Streckenprofil passen und ob der
    Ladestand zwischen den Stopps irgendwo unter die Reserve rutscht - auch
    dort, wo gar kein Stopp geplant ist.
    """
    soc_per_kwh = 100.0 / fz.battery_net_kwh
    soc = start_soc
    lowest = soc
    largest_deviation = 0.0
    brand_km = 0.0
    brand_kwh = profile.val(profile.kwh, 0.0)

    stations = [(s.option.km_on_route, s) for s in plan.stops]
    stations.append((profile.total_km, None))

    for target_km, stop in stations:
        for i, k in enumerate(profile.km):
            if brand_km <= k <= target_km:
                lowest = min(lowest, soc - (profile.kwh[i] - brand_kwh) * soc_per_kwh)
        arrival = soc - (profile.val(profile.kwh, target_km) - brand_kwh) * soc_per_kwh
        lowest = min(lowest, arrival)
        if stop is None:
            soc = arrival
            break
        largest_deviation = max(largest_deviation,
                                  abs(arrival - stop.arrival_soc))
        soc = stop.departure_soc
        brand_km = target_km
        brand_kwh = profile.val(profile.kwh, target_km)

    return {"lowest_soc": lowest, "soc_at_target": soc,
            "deviation": largest_deviation}


def examine_plan(plan, profile, fz, start_soc, target_soc, name: str) -> dict:
    """Die Prüfungen, die für jeden machbaren Plan gelten müssen."""
    facts = recompute(plan, profile, fz, start_soc)
    verify(facts["lowest_soc"] >= fz.reserve_soc - 0.5,
           f"{name}: der Ladestand fällt nirgends unter die Reserve",
           f"tiefster {facts['lowest_soc']:.1f} %, Reserve {fz.reserve_soc} %")
    verify(facts["soc_at_target"] >= target_soc - 0.5,
           f"{name}: der Ziel-Ladestand wird erreicht",
           f"{facts['soc_at_target']:.1f} % statt {target_soc} %")
    verify(facts["deviation"] < 0.6,
           f"{name}: die ausgewiesenen Ankunftswerte stimmen mit der Strecke überein",
           f"grösste Abweichung {facts['deviation']:.2f} Prozentpunkte")
    amount_sum = (plan.drive_time_minutes + plan.charge_time_minutes
             + plan.detour_time_minutes + plan.holding_cost_minutes)
    verify(abs(amount_sum - plan.total_minutes) < 0.5,
           f"{name}: Fahrzeit, Ladezeit, Umwege und Haltekosten ergeben die "
           f"Gesamtzeit")
    verify(abs(plan.holding_cost_minutes
               - len(plan.stops) * optimizer.STOP_FIXED_COST_MIN) < 0.01,
           f"{name}: die Haltekosten hängen an der Zahl der Stopps, nicht an "
           f"der Lademenge",
           f"{plan.holding_cost_minutes:.1f} min bei {len(plan.stops)} Stopps")

    # Kein Stopp, der sich nicht lohnt. Ein Halt kostet allein an Fixkosten
    # rund vier Minuten; wer dafür zwei Prozentpunkte lädt, hätte dieselbe
    # Energie am nächsten Stopp in weniger Zeit bekommen. Solche Stopps
    # entstanden, als die Nachoptimierung die Ladehübe auf das gerade noch
    # Nötige herunterschliff - der Suche waren sie verboten, dem
    # Nachoptimierer nicht.
    hills = [s.departure_soc - s.arrival_soc for s in plan.stops]
    verify(all(h >= optimizer.MIN_CHARGE_SWING - 0.5 for h in hills),
           f"{name}: an keinem Stopp wird weniger als der Mindesthub geladen",
           f"Ladehübe {[round(h, 1) for h in hills]}, "
           f"Mindesthub {optimizer.MIN_CHARGE_SWING}")
    return facts


# ---------------------------------------------------------------------------
# Die Fälle
# ---------------------------------------------------------------------------

def case_short_distance():
    section("Kurze Strecke - es braucht keinen Stopp")
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)
    profile = profile_build(150)
    plan = optimizer.schedule(profile, chargers([50, 100]), fz, CURVE,
                             start_soc=80.0, target_soc=20.0,
                             max_vehicle_kw=150.0)
    verify(plan.feasible, "feasible")
    verify(len(plan.stops) == 0, "und zwar ohne Ladestopp",
           f"{len(plan.stops)} Stopps geplant")
    verify(abs(plan.total_minutes - plan.drive_time_minutes) < 0.5,
           "die Gesamtzeit ist dann die reine Fahrzeit")


def case_long_distance():
    section("Langstrecke mit dichten Ladepunkten")
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)
    profile = profile_build(600)
    plan = optimizer.schedule(profile, chargers(range(50, 600, 50)), fz, CURVE,
                             start_soc=90.0, target_soc=20.0,
                             max_vehicle_kw=150.0)
    verify(plan.feasible, "feasible", plan.reason)
    verify(len(plan.stops) >= 2, "mehrere Stopps nötig",
           f"{len(plan.stops)} geplant")
    examine_plan(plan, profile, fz, 90.0, 20.0, "Langstrecke")

    # Der Zeitgewinn aus Abschnitt 2.4: lieber zweimal kurz in den steilen
    # Teil der Kurve als einmal lang bis 90 %.
    highest_departure = max(s.departure_soc for s in plan.stops)
    verify(highest_departure <= 80.0,
           "kein Stopp lädt in den flachen Teil der Kurve, solange Säulen dicht stehen",
           f"höchster Abfahrtswert {highest_departure:.0f} %")
    return plan


def case_strong_charger_wins():
    section("Wahl zwischen naher schwacher und weiterer starker Säule")
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)
    profile = profile_build(450)
    options = [
        optimizer.ChargeOption(id=1, km_on_route=200.0, detour_minutes=5.0,
                              max_kw=50.0, point_count=4, name="50 kW bei km 200"),
        optimizer.ChargeOption(id=2, km_on_route=240.0, detour_minutes=5.0,
                              max_kw=300.0, point_count=4, name="300 kW bei km 240"),
    ]
    plan = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=300.0)
    verify(plan.feasible, "feasible", plan.reason)
    chosen = [s.option.id for s in plan.stops]
    verify(chosen == [2],
           "die starke Säule 40 km weiter gewinnt gegen die nahe schwache",
           f"gewählt: {chosen}")
    examine_plan(plan, profile, fz, 90.0, 20.0, "Säulenwahl")


def fill_case_gap_earlier():
    section("Lange Lücke ohne Schnelllader - vorher volltanken")
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)
    profile = profile_build(700)
    # Nach km 260 kommt bis km 540 nichts mehr. Wer dort nur so weit lädt, wie
    # es sich gerade lohnt, steht in der Lücke - genau der Fall, an dem ein
    # gieriger Planer scheitert.
    options = chargers([100, 180, 260, 540, 620])
    plan = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                             target_soc=15.0, max_vehicle_kw=150.0)
    verify(plan.feasible, "feasible", plan.reason)
    examine_plan(plan, profile, fz, 90.0, 15.0, "Lücke")

    prior_gap = [s for s in plan.stops if abs(s.option.km_on_route - 260.0) < 1]
    verify(bool(prior_gap), "an der letzten Säule vor der Lücke wird gehalten")
    if prior_gap:
        verify(prior_gap[0].departure_soc >= 90.0,
               "und dort weit in den flachen Teil der Kurve geladen - "
               "die Lücke lässt nichts anderes zu",
               f"Abfahrt mit {prior_gap[0].departure_soc:.0f} %")


def case_pass():
    section("Pass - die Spitze zählt, nicht die Bilanz am Ende")
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)

    def kwh_at(km: float) -> float:
        # Bis km 150 hinauf auf 40 kWh, dann holt die Rekuperation bis km 300
        # zehn kWh zurück. Am Ziel stehen 30 kWh - das sähe machbar aus.
        if km <= 150.0:
            return km / 150.0 * 40.0
        return 40.0 - (km - 150.0) / 150.0 * 10.0

    profile = profile_build(300, kwh_at=kwh_at)
    soc_per_kwh = 100.0 / fz.battery_net_kwh
    net_required = fz.reserve_soc + 30.0 * soc_per_kwh
    peak_required = fz.reserve_soc + 40.0 * soc_per_kwh
    verify(net_required <= 60.0 < peak_required,
           "der Fall ist so gebaut, dass nur die Bilanz am Ziel machbar aussieht",
           f"netto {net_required:.0f} %, Spitze {peak_required:.0f} %, Start 60 %")

    without = optimizer.schedule(profile, [], fz, CURVE, start_soc=60.0,
                             target_soc=10.0, max_vehicle_kw=150.0)
    verify(not without.feasible,
           "ohne Ladepunkt wird die Strecke abgelehnt, nicht durchgewinkt")
    verify("Ladepunkt" in without.reason,
           "und der Grund benennt das fehlende Angebot", without.reason)

    plan = optimizer.schedule(profile, chargers([80], max_kw=150.0), fz, CURVE,
                             start_soc=60.0, target_soc=10.0,
                             max_vehicle_kw=150.0)
    verify(plan.feasible, "mit einem Ladepunkt vor dem Anstieg geht es", plan.reason)
    verify(len(plan.stops) == 1, "ein Stopp genügt",
           f"{len(plan.stops)} geplant")
    facts = examine_plan(plan, profile, fz, 60.0, 10.0, "Pass")
    verify(facts["lowest_soc"] >= fz.reserve_soc - 0.5,
           "auf der Passhöhe bleibt die Reserve stehen",
           f"tiefster {facts['lowest_soc']:.1f} %")


def case_redundancy():
    section("Redundanz - acht Ladepunkte schlagen einen")
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)
    profile = profile_build(450)
    options = [
        optimizer.ChargeOption(id=1, km_on_route=220.0, detour_minutes=5.0,
                              max_kw=150.0, point_count=1, name="einzeln"),
        optimizer.ChargeOption(id=2, km_on_route=222.0, detour_minutes=5.0,
                              max_kw=150.0, point_count=8, name="acht Punkte"),
    ]
    plan = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=150.0)
    chosen = [s.option.id for s in plan.stops]
    verify(chosen == [2],
           "bei sonst gleichen Standorten gewinnt der mit mehr Ladepunkten",
           f"gewählt: {chosen}")


def case_preferred_operator():
    section("Bevorzugter Anbieter - kein Ausschluss, nur ein Vorteil")
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)
    profile = profile_build(450)
    # Zwei Standorte an derselben Stelle der Route (z.B. zwei Ladeparks an
    # derselben Abfahrt), der bevorzugte mit drei Minuten mehr Umweg - weniger
    # als der Bonus von vier Minuten, aber genug, um ohne Vorgabe zu verlieren.
    options = [
        optimizer.ChargeOption(id=1, km_on_route=220.0, detour_minutes=4.0,
                              max_kw=150.0, point_count=1, name="fremd",
                              operator="Fremdanbieter GmbH"),
        optimizer.ChargeOption(id=2, km_on_route=220.0, detour_minutes=7.0,
                              max_kw=150.0, point_count=1, name="bevorzugt",
                              operator="EnBW mobility+"),
    ]
    without = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=150.0)
    verify([s.option.id for s in without.stops] == [1],
           "ohne Vorgabe gewinnt der Standort mit dem kleineren Umweg",
           f"gewählt: {[s.option.id for s in without.stops]}")

    using = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                            target_soc=20.0, max_vehicle_kw=150.0,
                            preferred_operators=["EnBW"])
    verify([s.option.id for s in using.stops] == [2],
           "mit Vorgabe gleicht der Bonus den grösseren Umweg aus",
           f"gewählt: {[s.option.id for s in using.stops]}")

    # Ein Standort mit 30 Minuten Umweg ist auch als bevorzugter Anbieter kein
    # Schnäppchen - der Bonus wiegt den Umweg auf, macht ihn aber nie gratis.
    far = [optimizer.ChargeOption(id=3, km_on_route=210.0, detour_minutes=30.0,
                                  max_kw=300.0, point_count=8,
                                  name="weit, aber bevorzugt",
                                  operator="EnBW mobility+"),
            optimizer.ChargeOption(id=4, km_on_route=215.0, detour_minutes=6.0,
                                  max_kw=150.0, point_count=4, name="nah")]
    plan_far = optimizer.schedule(profile, far, fz, CURVE, start_soc=90.0,
                                  target_soc=20.0, max_vehicle_kw=300.0,
                                  preferred_operators=["EnBW"])
    verify([s.option.id for s in plan_far.stops] == [4],
           "ein 30-Minuten-Umweg bleibt draussen, auch beim bevorzugten Anbieter",
           f"gewählt: {[s.option.id for s in plan_far.stops]}")


def case_preference_survives_thinning():
    section("Bevorzugter Anbieter übersteht die Ausdünnung eines dichten Abschnitts")
    # Vier Standorte im selben Streckenabschnitt (bei 450 km Gesamtstrecke ist
    # die Abschnittsbreite hier 22,5 km, alle vier liegen darin). Sortiert
    # nach Leistung landet der bevorzugte Standort auf Platz vier - und wäre
    # ohne Sonderbehandlung aus dem Abschnitt geflogen, bevor der
    # Betreiber-Bonus in _nachfolger() je zum Zug käme.
    options = [
        optimizer.ChargeOption(id=1, km_on_route=205.0, detour_minutes=5.0,
                              max_kw=300.0, name="stark 1"),
        optimizer.ChargeOption(id=2, km_on_route=208.0, detour_minutes=5.0,
                              max_kw=250.0, name="stark 2"),
        optimizer.ChargeOption(id=3, km_on_route=211.0, detour_minutes=5.0,
                              max_kw=200.0, name="stark 3"),
        optimizer.ChargeOption(id=4, km_on_route=214.0, detour_minutes=5.0,
                              max_kw=100.0, name="schwach, bevorzugt",
                              operator="Ionity"),
    ]
    without = optimizer._thin_out_candidates(options, 450.0,
                                             optimizer.DETOUR_LIMIT_MIN)
    verify([o.id for o in without] == [1, 2, 3],
           "ohne Vorgabe fällt der schwächste Standort aus dem Abschnitt",
           f"behalten: {[o.id for o in without]}")

    using = optimizer._thin_out_candidates(
        options, 450.0, optimizer.DETOUR_LIMIT_MIN,
        preferred_operators=["Ionity"])
    verify([o.id for o in using] == [1, 2, 4],
           "mit Vorgabe verdrängt der bevorzugte Standort den schwächsten der Gruppe",
           f"behalten: {[o.id for o in using]}")


def case_exclusions():
    section("Was nicht in den Plan darf")
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)
    profile = profile_build(450)

    near_expensive = [
        optimizer.ChargeOption(id=1, km_on_route=200.0, detour_minutes=30.0,
                              max_kw=300.0, point_count=8, name="30 min Umweg"),
        optimizer.ChargeOption(id=2, km_on_route=210.0, detour_minutes=6.0,
                              max_kw=150.0, point_count=4, name="6 min Umweg"),
    ]
    plan = optimizer.schedule(profile, near_expensive, fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=300.0)
    chosen = [s.option.id for s in plan.stops]
    verify(chosen == [2],
           "ein Standort mit 30 Minuten Umweg fällt raus, so stark er auch ist",
           f"gewählt: {chosen}")

    reported = [
        optimizer.ChargeOption(id=1, km_on_route=210.0, detour_minutes=5.0,
                              max_kw=300.0, point_count=8, name="belegt",
                              locked=True),
        optimizer.ChargeOption(id=2, km_on_route=215.0, detour_minutes=5.0,
                              max_kw=150.0, point_count=4, name="frei"),
    ]
    plan = optimizer.schedule(profile, reported, fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=300.0)
    chosen = [s.option.id for s in plan.stops]
    verify(chosen == [2],
           "ein als belegt gemeldeter Standort wird nicht eingeplant",
           f"gewählt: {chosen}")

    empty = optimizer.schedule(profile, [], fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=150.0)
    verify(not empty.feasible, "ohne Kandidaten ist die Strecke nicht machbar")
    verify("importiert" in empty.reason,
           "und der Grund weist auf die womöglich leere Tabelle hin", empty.reason)


def case_alternative():
    section("Ausweichstandorte")
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)
    profile = profile_build(600)
    plan = optimizer.schedule(profile, chargers(range(40, 600, 20)), fz, CURVE,
                             start_soc=90.0, target_soc=20.0,
                             max_vehicle_kw=150.0)
    verify(plan.feasible, "feasible", plan.reason)
    with_alternative = [s for s in plan.stops if s.detour_alt]
    verify(bool(with_alternative),
           "bei dicht stehenden Säulen hat mindestens ein Stopp einen Ausweichstandort",
           f"{len(with_alternative)} von {len(plan.stops)}")

    soc_per_kwh = 100.0 / fz.battery_net_kwh
    consistent = True
    for stop in with_alternative:
        detour_alt = stop.detour_alt
        check_km = detour_alt["km_on_route"] > stop.option.km_on_route
        demand = (profile.val(profile.kwh, detour_alt["km_on_route"])
                  - profile.val(profile.kwh, stop.option.km_on_route)) * soc_per_kwh
        # Ohne Nachladen erreichbar heisst: mit dem Ladestand bei Ankunft, und
        # dabei darf höchstens die halbe Reserve angebrochen werden.
        if not check_km or stop.arrival_soc - demand < fz.reserve_soc / 2.0 - 0.5:
            consistent = False
    verify(consistent,
           "jeder Ausweichstandort liegt voraus und ist ohne Nachladen erreichbar")


def case_density_chargers():
    section("Dichte Säulen und hoher Verbrauch - keine Alibi-Stopps")
    # Der Fall, in dem der Mindestladehub gebraucht wird: viele erreichbare
    # Säulen, hoher Verbrauch, und darunter grosse Ladeparks mit kräftigem
    # Redundanzbonus. Solange der Bonus Umweg *und* Ladezeit aufwiegen durfte
    # und die Nachoptimierung den Mindesthub nicht kannte, entstand hier ein
    # Halt über fünf Prozentpunkte in anderthalb Minuten - rechnerisch billig,
    # in Wirklichkeit ein Umweg für nichts.
    fz = VehicleValues(battery_net_kwh=60.0, reserve_soc=10.0)
    profile = profile_build(577, kwh_per_km=0.257)
    pattern = [(150, 4), (300, 8), (350, 12), (150, 6)]
    options = []
    for i, km in enumerate(range(35, 577, 35)):
        kw, points = pattern[i % len(pattern)]
        options.append(optimizer.ChargeOption(
            id=i + 1, km_on_route=float(km), detour_minutes=5.1,
            max_kw=float(kw), point_count=points, name=f"Rasthof km {km}"))

    plan = optimizer.schedule(profile, options, fz, CURVE, start_soc=65.0,
                             target_soc=20.0, max_vehicle_kw=350.0)
    verify(plan.feasible, "feasible", plan.reason)
    # Hier stand "mindestens sechs Stopps" - und diese Erwartung war selbst
    # ein Symptom. Ohne Fixkosten je Halt plante der Optimierer diese Strecke
    # mit **zehn** Stopps, sechs davon drei bis vier Minuten lang, und war
    # damit real neunundvierzig Minuten langsamer als die vier Halte, die
    # jetzt herauskommen. Die Strecke braucht viel Energie, nicht viele Halte:
    # 577 km bei 0,257 kWh/km sind knapp 150 kWh in einen 60-kWh-Akku.
    verify(len(plan.stops) >= 3, "die Strecke braucht mehrere Stopps",
           f"{len(plan.stops)}")
    examine_plan(plan, profile, fz, 65.0, 20.0, "Dichte Säulen")

    short = [s for s in plan.stops if s.charge_time_minutes < 2.0]
    verify(not short,
           "kein Stopp dauert unter zwei Minuten - dafür lohnt kein Abstecher",
           str([(s.option.name, s.charge_time_minutes) for s in short]))


def case_fragmentation():
    section("Fixkosten je Halt - wenige lange statt vieler kurzer Stopps")
    # Der Fehler, den dieser Fall festhält: Die Zielfunktion zählte Ladezeit
    # und Umweg, aber nichts, was an der blossen *Anzahl* der Stopps hängt.
    # Weil ein Akku bei 10 % viel schneller lädt als bei 60 %, ist es unter
    # dieser Annahme immer günstiger, dieselbe Energie auf viele kurze Halte
    # bei niedrigem Ladestand zu verteilen. Auf einer echten Fahrt
    # (Le Gurp - Montalivet, 662 km) kamen so vier Stopps heraus, drei davon
    # unter vier Minuten.
    fz = VehicleValues(battery_net_kwh=77.0, reserve_soc=10.0)
    profile = profile_build(660, kwh_per_km=0.21)
    options = chargers(range(60, 660, 40), max_kw=300.0, detour=4.0,
                       point_count=8)

    shared = dict(start_soc=80.0, target_soc=20.0, max_vehicle_kw=200.0)
    without = optimizer.schedule(profile, options, fz, CURVE,
                             stop_fixed_cost_min=0.0, **shared)
    using = optimizer.schedule(profile, options, fz, CURVE, **shared)

    verify(without.feasible and using.feasible, "beide Varianten sind machbar",
           f"{without.reason} / {using.reason}")
    verify(len(using.stops) < len(without.stops),
           "mit Fixkosten je Halt entstehen weniger Stopps",
           f"{len(using.stops)} statt {len(without.stops)}")

    def actual_time(plan) -> float:
        """Was der Plan **tatsächlich** kostet - Haltekosten inbegriffen.

        Der Massstab, an dem sich beide messen lassen müssen. Der alte Plan
        war nie schneller, er sah nur schneller aus: Ein Teil seiner Kosten
        stand nicht in der Rechnung.
        """
        return (plan.drive_time_minutes + plan.charge_time_minutes
                + plan.detour_time_minutes
                + len(plan.stops) * optimizer.STOP_FIXED_COST_MIN)

    verify(actual_time(using) < actual_time(without) - 1.0,
           "und der neue Plan ist an der Wirklichkeit gemessen schneller - "
           "der alte war nur billiger gerechnet",
           f"{actual_time(using):.0f} min gegen {actual_time(without):.0f} min")

    short = [s for s in using.stops
            if s.charge_time_minutes < optimizer.STOP_FIXED_COST_MIN]
    verify(not short,
           "kein Halt dauert kürzer, als er an Fixkosten kostet - für so "
           "einen Stopp lohnt das Abfahren nie",
           str([(s.option.name, round(s.charge_time_minutes, 1)) for s in short]))

    examine_plan(profile=profile, plan=using, fz=fz, start_soc=80.0,
                 target_soc=20.0, name="Fixkosten")


def case_charge_park():
    section("Grosser Ladepark - Gutschrift wirkt auch ohne Umweg")
    # Zwei Fehler, die zusammen dafür sorgten, dass die Bevorzugung
    # ausgerechnet an der Autobahn nicht wirkte.
    from app.charging.availability import redundancy_bonus

    # (1) Die Gutschrift sättigte bei rund 15 Punkten. In den Daten einer
    # Frankreich-Route bekamen Standorte mit 15, 17, 20, 28 und 30
    # Ladepunkten alle exakt denselben Wert - die Grösse hörte genau dort
    # auf zu zählen, wo die interessanten Parks anfangen.
    verify(redundancy_bonus(30) > redundancy_bonus(15) + 0.3,
           "dreissig Ladepunkte zählen mehr als fünfzehn",
           f"{redundancy_bonus(30)} gegen {redundancy_bonus(15)}")
    verify(redundancy_bonus(4) > redundancy_bonus(2) + 0.3,
           "und der Sprung von zwei auf vier zählt am meisten",
           f"{redundancy_bonus(2)} → {redundancy_bonus(4)}")
    verify(redundancy_bonus(30, 0.0) == 0.0,
           "auf null gestellt gibt es keine Gutschrift - dann zählt die Uhr")

    # (2) Der eigentliche Fehler: Die Gutschrift war am Umweg gedeckelt
    # (`min(bonus, umweg)`). Ein Ladepark **direkt an der Route** hat keinen
    # Umweg - also bekam er auch keine Gutschrift, obwohl genau dort die
    # grossen Parks stehen.
    fz = VehicleValues(battery_net_kwh=77.0, reserve_soc=10.0)
    profile = profile_build(400, kwh_per_km=0.21)
    # Zwei Standorte fast an derselben Stelle, beide ohne Umweg: einer
    # gross, einer klein. Ohne Gutschrift entscheidet der Zufall der
    # Kandidatenreihenfolge.
    options = [
        optimizer.ChargeOption(id=1, km_on_route=200.0, detour_minutes=0.0,
                              max_kw=150.0, point_count=2,
                              name="Zwei Säulen", operator="Klein"),
        optimizer.ChargeOption(id=2, km_on_route=203.0, detour_minutes=0.0,
                              max_kw=150.0, point_count=30,
                              name="Grosser Park", operator="Gross"),
    ]
    plan = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=150.0)
    verify(plan.feasible and plan.stops, "feasible", plan.reason)
    verify(plan.stops[0].option.id == 2,
           "bei gleichem Umweg gewinnt der grosse Ladepark",
           f"gewählt wurde {plan.stops[0].option.name}")

    without = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=150.0,
                             charge_park_bonus_min=0.0)
    verify(without.feasible,
           "und auf null gestellt bleibt der Plan trotzdem gültig",
           without.reason)

    # Die Gutschrift darf die Ladezeit nie aufwiegen - ein Halt kostet
    # mindestens so viel, wie das Laden dauert.
    for_a = [options[1]]
    p2 = optimizer.schedule(profile, for_a, fz, CURVE, start_soc=90.0,
                           target_soc=20.0, max_vehicle_kw=150.0,
                           charge_park_bonus_min=12.0)
    if p2.feasible and p2.stops:
        pause = (p2.total_minutes - p2.drive_time_minutes)
        verify(pause > 0,
               "auch mit grosser Gutschrift kostet ein Halt noch Zeit",
               f"{pause:.1f} min für {len(p2.stops)} Stopps")


def case_cost():
    section("Kosten gegen Zeit - der Handel, der vorher nicht auszudrücken war")
    # Der Optimierer minimierte ausschliesslich Zeit. Ein Anbieterwunsch war
    # deshalb nur als Zeitgutschrift auszudrücken - eine Vorliebe, als
    # Minuten verkleidet. Der eigentliche Handel ("länger laden, dafür
    # billiger") liess sich damit gar nicht formulieren.
    from app.charging.prices import price_per_kwh

    verify(price_per_kwh("Ionity GmbH", [{"pattern": "Ionity", "eur_kwh": 0.39}],
                        0.69) == 0.39,
           "der Anbieter wird als Teilzeichenkette getroffen")
    verify(price_per_kwh("Irgendwer", [{"pattern": "Ionity", "eur_kwh": 0.39}],
                        0.69) == 0.69,
           "und alles Übrige bekommt den Standardpreis")

    fz = VehicleValues(battery_net_kwh=77.0, reserve_soc=10.0)
    profile = profile_build(500, kwh_per_km=0.21)
    # Zwei gleichwertige Standorte an derselben Stelle - einer teuer, einer
    # billig. Ohne Kosten in der Zielfunktion entscheidet der Zufall.
    options = [
        optimizer.ChargeOption(id=1, km_on_route=250.0, detour_minutes=0.0,
                              max_kw=150.0, point_count=8,
                              name="Teuer", operator="Teuer"),
        optimizer.ChargeOption(id=2, km_on_route=252.0, detour_minutes=0.0,
                              max_kw=150.0, point_count=8,
                              name="Billig", operator="Ionity"),
    ]
    price = lambda o: 0.39 if "ionity" in (o.operator or "").lower() else 0.79

    ignored = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=150.0,
                             price_for=price, time_value_eur_h=0.0)
    using = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                            target_soc=20.0, max_vehicle_kw=150.0,
                            price_for=price, time_value_eur_h=30.0)
    verify(ignored.feasible and using.feasible, "beide Pläne sind machbar",
           f"{ignored.reason} / {using.reason}")
    verify(using.stops and using.stops[0].option.operator == "Ionity",
           "mit Kostengewicht gewinnt bei gleicher Zeit der billigere Anbieter",
           str([s.option.operator for s in using.stops]))
    verify(using.cost_eur < ignored.cost_eur + 0.01,
           "und der Plan ist nicht teurer als der rein zeitoptimale",
           f"{using.cost_eur:.2f} gegen {ignored.cost_eur:.2f} EUR")
    verify(ignored.cost_eur > 0,
           "die Kosten stehen auch dann im Plan, wenn sie nicht optimiert "
           "werden - sonst wüsste niemand, was der Plan kostet",
           f"{ignored.cost_eur:.2f} EUR")

    # Der Zeitwert muss die Richtung umdrehen können: Wer seine Stunde sehr
    # hoch bewertet, nimmt den teureren Strom in Kauf, wenn er Zeit spart.
    amount_sum = sum(s.kwh_charged for s in using.stops)
    verify(amount_sum > 0 and abs(using.cost_eur - amount_sum * 0.39) < 0.05,
           "die ausgewiesenen Kosten passen zur geladenen Energie",
           f"{amount_sum:.1f} kWh, {using.cost_eur:.2f} EUR")

    # Die Nachoptimierung (Schritt 4) läuft **nach** der Suche und
    # überschreibt deren Lademengen. Kennt sie die Kosten nicht, verschiebt
    # sie Energie von der billigen Säule zur teuren, sobald das Sekunden
    # spart - und macht damit still zunichte, was Schritt 3 gerade
    # optimiert hat.
    cheap_then_expensive = [
        optimizer.ChargeOption(id=1, km_on_route=170.0, detour_minutes=0.0,
                              max_kw=150.0, point_count=8,
                              name="Billig", operator="Ionity"),
        optimizer.ChargeOption(id=2, km_on_route=340.0, detour_minutes=0.0,
                              max_kw=150.0, point_count=8,
                              name="Teuer", operator="Teuer"),
    ]
    long = profile_build(500, kwh_per_km=0.21)
    p3 = optimizer.schedule(long, cheap_then_expensive, fz, CURVE, start_soc=60.0,
                           target_soc=20.0, max_vehicle_kw=150.0,
                           price_for=price, time_value_eur_h=20.0)
    if p3.feasible and len(p3.stops) == 2:
        cheap, expensive = p3.stops[0], p3.stops[1]
        verify(cheap.kwh_charged > expensive.kwh_charged,
               "auch nach der Nachoptimierung liegt das Gewicht auf der "
               "billigen Säule - Schritt 4 darf Schritt 3 nicht widersprechen",
               f"billig {cheap.kwh_charged:.1f} kWh, "
               f"teuer {expensive.kwh_charged:.1f} kWh")


def case_runtime():
    section("Laufzeit")
    fz = VehicleValues(battery_net_kwh=77.0, reserve_soc=10.0)
    profile = profile_build(900)
    # 180 Kandidaten, wie sie eine echte Korridorsuche entlang einer
    # Langstrecke liefert. Der Optimierer dünnt sie selbst aus.
    options = chargers(range(20, 900, 5), max_kw=150.0, point_count=4)
    begun = time.perf_counter()
    plan = optimizer.schedule(profile, options, fz, CURVE, start_soc=90.0,
                             target_soc=20.0, max_vehicle_kw=150.0)
    duration = time.perf_counter() - begun
    verify(plan.feasible, "900 km mit 176 Kandidaten sind planbar", plan.reason)
    verify(duration < 2.0, "und zwar in unter zwei Sekunden",
           f"gebraucht: {duration:.2f} s")
    verify(plan.checked_candidates <= optimizer.AT_MOST_CANDIDATES,
           "die Kandidaten werden auf die Obergrenze ausgedünnt",
           f"{plan.checked_candidates} übrig")
    examine_plan(plan, profile, fz, 90.0, 20.0, "Laufzeit")
    print(f"        ({duration * 1000:.0f} ms, {len(plan.stops)} Stopps, "
          f"{plan.total_minutes:.0f} min gesamt)")


def main() -> int:
    case_short_distance()
    case_long_distance()
    case_strong_charger_wins()
    fill_case_gap_earlier()
    case_pass()
    case_redundancy()
    case_preferred_operator()
    case_preference_survives_thinning()
    case_exclusions()
    case_alternative()
    case_density_chargers()
    case_fragmentation()
    case_charge_park()
    case_cost()
    case_runtime()

    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
