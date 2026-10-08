"""Die Live-Nachführung: Ist gegen Soll, während gefahren wird.

Der Grund für das ganze Projekt. Ein Plan, der bei Abfahrt gerechnet wurde,
ist nach achtzig Kilometern falsch - Tempo, Temperatur, Wind und Stau
addieren sich in dieselbe Richtung. Wer das merkt, braucht keinen Puffer von
zwanzig Prozent; wer es nicht merkt, steht mit vier Prozent an einer belegten
Säule.

Was hier passiert: Zu jedem Messpunkt wird bestimmt, wo auf der Route er
liegt, was der Plan an dieser Stelle vorhergesagt hatte, und wie weit die
Wirklichkeit davon abweicht. Daraus entstehen zwei laufende Faktoren - einer
für den Verbrauch, einer für die Zeit - und aus ihnen die Frage, ob der
Ladeplan noch stimmt. Tut er das nicht, wird er neu gerechnet
(`live/replanning.py`).

Neu geplant wird bewusst nicht bei jeder Messung, sondern nur, wenn einer der
Auslöser aus Abschnitt 2.3 des Konzepts greift. Ein Plan, der sich alle
dreissig Sekunden ändert, ist kein Plan.
"""
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .. import models
from ..energy import charge_phases
from ..energy.profile import minutes_at as plan_minutes_at
from ..energy.profile import soc_at as plan_soc_at
from ..charging import availability
from ..routing.corridor import point_on_route
from . import replanning

log = logging.getLogger("uvicorn.error")

# Schwellen für die Neuplanung, eins zu eins aus Abschnitt 2.3 des Konzepts.
# Bewusst Schwellen und keine Neuberechnung bei jeder Messung: Wer gerade
# beschlossen hat, in 40 km Pause zu machen, soll das nicht dreimal umwerfen
# müssen. Eine Änderung muss etwas bedeuten.
THRESHOLD_SOC_PP = 5.0            # Prozentpunkte Abweichung
THRESHOLD_DETOUR_M = 500.0         # Abstand zur Route
THRESHOLD_DETOUR_S = 60.0          # ... und wie lange er anhalten muss
THRESHOLD_ARRIVAL_MIN = 10.0      # Verschiebung der Ankunftszeit

# Wie weit gefahren sein muss, bevor derselbe nicht-dringende Auslöser erneut
# eine Neuplanung anstösst. Ohne diese Sperre rechnete jede Messung neu,
# solange die Abweichung besteht - und das ist der Normalfall, nicht die
# Ausnahme.
REPLANNING_SPACING_KM = 10.0

# Über wie viele Kilometer die Faktoren gemittelt werden. Zu kurz, und eine
# einzelne Ampelphase verbiegt sie; zu lang, und der Wetterumschwung hinter
# dem Pass kommt zu spät an.
TIMEFRAME_KM = 25.0
# Vorher ist die SoC-Anzeige (meist 1 % Auflösung) zu grob für eine Aussage.
MIN_DISTANCE_KM = 5.0


@dataclass
class State:
    km_on_route: float
    spacing_to_route_m: float
    # Wo der Messpunkt lag.
    #
    # Ohne diese beiden musste die Oberflaeche die Position aus dem
    # *geplanten* Profil zurueckrechnen ("welcher Stuetzpunkt liegt bei
    # km X"). Bei einer **Aufzeichnung** gibt es dieses Profil nicht - es
    # entsteht erst beim Abschliessen -, und die Rueckrechnung lieferte
    # stumm (0, 0). Die Karte zeigte den Golf von Guinea, die gefahrene
    # Spur bestand aus einem einzigen Punkt, und die Verlaufskurve, die
    # ihre x-Achse entlang dieser Spur misst, fiel zu einem senkrechten
    # Strich zusammen - ausgerechnet bei der Betriebsart, in der die Kurve
    # das Einzige ist, was es zu sehen gibt.
    #
    # Der Server weiss die Koordinate; er hat sie gerade entgegengenommen.
    lat: float
    lon: float
    # Der Ladestand, mit dem gerechnet wird - gemeldet oder hochgerechnet.
    # `soc_gemeldet` sagt, welches von beidem: Wer am Steuer eine Zahl sieht,
    # soll wissen, ob sie gemessen oder aus dem Profil gerechnet ist.
    actual_soc: float | None
    soc_reported: bool
    plan_soc: float | None
    deviation_pp: float | None
    consumption_factor: float
    time_factor: float
    remaining_km: float
    forecast_soc_at_target: float | None
    reserve_at_km: float | None
    arrival_shift_min: float | None
    next_stop: dict | None
    replanning_required: bool
    reason: str
    # Wird nur gesetzt, wenn tatsächlich neu geplant wurde.
    plan: dict | None = None
    plan_changed: bool = False
    change: str = ""
    urgent: bool = field(default=False, repr=False)
    # Woher `ist_soc` kommt: "gemessen" (dieser Punkt), "gerechnet" (aus dem
    # Profil) oder "zuletzt" (die letzte Messung dieser Fahrt, weil dieser Punkt
    # keine hat und es kein Profil gibt - der Fall einer Aufzeichnung, solange
    # das Auto nicht antwortet).
    soc_source: str = "gemessen"


# ---------------------------------------------------------------------------
# Die beiden laufenden Faktoren
# ---------------------------------------------------------------------------

def _timeframe(points: list) -> list:
    """Die Messpunkte der letzten `FENSTER_KM`, mindestens aber zwei."""
    usable = [p for p in points
                 if p.km_on_route is not None and p.plan_soc is not None]
    if len(usable) < 2:
        return []
    last = usable[-1]
    timeframe = [p for p in usable
               if (last.km_on_route - p.km_on_route) <= TIMEFRAME_KM]
    return timeframe if len(timeframe) >= 2 else usable[-2:]


def _charge_pauses_minutes(points: list, energy_profile: list) -> float:
    """Wie viel der verstrichenen Zeit auf Ladepausen entfiel.

    Nötig, weil das Energieprofil ausschliesslich **Fahrzeit** führt: Die
    Ladezeit steht im Plan, nie im Profil. Wer die Wanduhr ungefiltert gegen
    das Profil hält, sieht deshalb nach dem ersten Ladestopp eine Verspätung
    in Höhe der Ladedauer - und zwar dauerhaft, denn sie wird nie wieder
    aufgeholt. Damit stünde der Auslöser "Ankunft verschiebt sich" für den
    Rest der Fahrt über seiner Schwelle und meldete alle zehn Kilometer
    dieselbe Verspätung. Eine Meldung, die immer kommt, schaltet man ab.

    Erkannt wird die Pause am steigenden Ladestand: Beim Fahren fällt er,
    beim Laden steigt er. Gezählt wird aber nicht die ganze Zeitspanne,
    sondern nur der Teil, der über der Fahrzeit für die dabei zurückgelegte
    Strecke liegt. Das erledigt zwei Fälle auf einmal - Rekuperation auf
    langer Talfahrt hebt den Ladestand zwar auch, kostet aber keine
    zusätzliche Zeit; und es ist gleichgültig, ob der Logger während des
    Ladens weitergesendet hat oder erst hinterher wieder aufgewacht ist.

    Nicht abgezogen wird eine Pause ohne Ladung - Mittagessen, Stau, Stau vor
    der Baustelle. Die verschiebt die Ankunft wirklich, und genau das soll
    der Auslöser sehen.
    """
    def driven(from_km: float, until_km: float) -> float:
        begin = plan_minutes_at(energy_profile, from_km)
        upto = plan_minutes_at(energy_profile, until_km)
        return 0.0 if begin is None or upto is None else max(0.0, upto - begin)

    return charge_phases.charge_pauses_minutes(points, driven)


def _consumption_factor(points: list) -> float | None:
    """Ist-Verbrauch geteilt durch Soll-Verbrauch über das gleitende Fenster.

    Gerechnet wird über SoC-Differenzen und nicht über absolute Werte: Ein
    Tacho, der grundsätzlich zwei Prozent zu hoch anzeigt, verfälscht die
    Differenz nicht - den absoluten Vergleich aber schon.

    Nur gemeldete Ladestände zählen. Ein geschätzter Wert hier hiesse, das
    Modell gegen sich selbst zu messen: Der Faktor käme immer auf 1,0 heraus
    und behauptete damit, die Prognose stimme - und zwar umso überzeugter, je
    länger niemand mehr nachgesehen hat.
    """
    # Erst filtern, dann fenstern - nicht umgekehrt. Wer den Ladestand nur an
    # Ladestopps eintippt, hat zwei Meldungen im Abstand von zweihundert
    # Kilometern; ein Fenster von 25 km über *alle* Punkte enthielte davon
    # keine zwei und der Faktor käme nie zustande. Über den gemeldeten
    # Ladeständen greift stattdessen die Rückfallregel in `_fenster` und nimmt
    # die letzten beiden - eine lange Messbasis ist hier sogar die bessere.
    timeframe = _timeframe([p for p in points if p.soc is not None])
    if not timeframe:
        return None

    first, last = timeframe[0], timeframe[-1]
    if last.km_on_route - first.km_on_route < MIN_DISTANCE_KM:
        return None

    # Ladeabschnitte fallen heraus, statt den Faktor unbrauchbar zu machen.
    # Vorher stand hier `erster.soc - letzter.soc`, und ein Ladestopp im
    # Fenster ergab einen negativen "Verbrauch" - abgefangen nur durch die
    # Ausreisserschranke darunter, die den Faktor dann verwarf. Die Folge:
    # Nach jedem Ladestopp galt für die Dauer des Fensters weiter der alte
    # Wert, obwohl frisch gemessen wurde.
    actual_consumption = 0.0
    plan_consumption = 0.0
    for section in charge_phases.sections(timeframe):
        if section.charges:
            continue
        actual_consumption += section.soc_pp
        plan_consumption += ((section.begin.plan_soc or 0.0)
                           - (section.past.plan_soc or 0.0))
    if plan_consumption <= 0.5:
        return None

    factor = actual_consumption / plan_consumption
    # Die Schranke bleibt als Netz: Sie fängt jetzt nur noch echte
    # Ausreisser ab - einen umgesteckten Logger, einen SoC-Sprung nach einem
    # Neustart -, nicht mehr den Normalfall Ladestopp.
    if not 0.4 <= factor <= 2.5:
        return None
    return round(factor, 3)


def soc_estimate(points: list, point, consumption_factor: float) -> float | None:
    """Der Ladestand an dieser Stelle, wenn keiner gemeldet wurde.

    Das ist der Kern des Betriebs ohne Fahrzeugdaten: Position liefert das
    Telefon dauernd, den Ladestand tippt jemand gelegentlich ein, und
    dazwischen trägt das Energieprofil. Das kennt Steigung, Tempo und Wetter
    der Strecke - es ist genau das Modell, auf dem auch der Ladeplan steht -,
    und der gemessene Verbrauchsfaktor sagt, wie weit das Auto davon abweicht.

    Keine Vorhersage also, sondern dieselbe Fortschreibung, mit der die
    Umplanung ohnehin rechnet. Und sie wird an jedem eingetippten Ladestand
    wieder auf die Wirklichkeit zurückgeholt.

    Gerechnet wird ab der **letzten Meldung** und nicht ab dem Start: Wer
    unterwegs geladen hat, hat einen Sprung im Ladestand, den kein Profil
    kennt. Die letzte Meldung liegt hinter diesem Sprung.
    """
    if point.plan_soc is None:
        return None
    previous = [p for p in points
                 if p.soc is not None and p.plan_soc is not None
                 and p is not point]
    if not previous:
        # Noch nie einen Ladestand gemeldet - dann gilt der geplante. Das
        # Profil beginnt beim Startladestand der Fahrt, also ist genau das
        # die einzige Aussage, die überhaupt vorliegt. Ohne diesen Rückfall
        # bliebe die ganze Anzeige leer, bis jemand von sich aus etwas
        # eintippt, und die Nachführung wäre für den ahnungslosen Fall aus.
        return point.plan_soc
    tail = previous[-1]
    consumed_plan = (tail.plan_soc or 0.0) - point.plan_soc
    return round(tail.soc - consumed_plan * consumption_factor, 2)


def _time_factor(points: list, energy_profile: list) -> float | None:
    """Ist-Fahrzeit geteilt durch Soll-Fahrzeit über dasselbe Fenster.

    Der eigene Faktor ist nötig, weil der Verbrauch einen Stau nicht sieht:
    Wer steht, verbraucht je Kilometer sogar etwas mehr, aber die Ankunftszeit
    verschiebt sich um ein Vielfaches davon. Ohne diese Zahl wäre der Auslöser
    "Ankunftszeit verschiebt sich" nicht zu haben - und jede Ankunftszeit im
    umgeplanten Ladeplan wäre die aus dem alten Plan.
    """
    timeframe = _timeframe(points)
    if not timeframe:
        return None

    first, last = timeframe[0], timeframe[-1]
    if last.km_on_route - first.km_on_route < MIN_DISTANCE_KM:
        return None
    if not first.timestamp or not last.timestamp:
        return None

    # Die Ladezeit gehört nicht in den Zeitfaktor: Wer eine halbe Stunde an
    # der Säule stand, hat keinen Stau. Ohne den Abzug wäre der Faktor nach
    # jedem Ladestopp so gross, dass ihn die Schranke unten verwirft - und
    # damit für die nächsten FENSTER_KM eingefroren, also genau auf der
    # Strecke blind, auf der er wieder gebraucht wird.
    actual_minutes = ((last.timestamp - first.timestamp).total_seconds() / 60.0
                   - _charge_pauses_minutes(timeframe, energy_profile))
    plan_end = plan_minutes_at(energy_profile, last.km_on_route)
    plan_start = plan_minutes_at(energy_profile, first.km_on_route)
    if plan_end is None or plan_start is None:
        return None
    plan_minutes = plan_end - plan_start
    if plan_minutes <= 0.5 or actual_minutes <= 0:
        return None

    factor = actual_minutes / plan_minutes
    # Dieselbe Ausreisserschranke wie beim Verbrauch - jetzt nur noch gegen
    # das, was der Ladepausen-Abzug nicht erklärt.
    if not 0.4 <= factor <= 3.0:
        return None
    return round(factor, 3)


# ---------------------------------------------------------------------------
# Messpunkt herein
# ---------------------------------------------------------------------------

def record_sample(db, session: models.LiveSession, lat: float, lon: float,
                        soc: float | None = None, speed_kmh: float | None = None,
                        outside_temp_c: float | None = None,
                        timestamp: datetime | None = None,
                        raw_values: dict | None = None,
                        new_plan: bool = True) -> State:
    """Einen Messpunkt einsortieren und den neuen Zustand zurückgeben.

    `soc` darf fehlen. Dann ist es eine reine Positionsmeldung, wie sie das
    Telefon im Sekundentakt liefern kann - der Ladestand wird für diesen
    Punkt aus dem Energieprofil hochgerechnet (`soc_schaetzen`). Nur so
    kommen Zeitfaktor und Ankunftsprognose überhaupt zustande, solange das
    Auto seinen Ladestand nicht selbst meldet.

    `zeit` überschreibt den Zeitstempel. Gebraucht wird das vom Simulator: Er
    spielt Stunden in Sekunden ab, und mit echten Uhrzeiten wäre der
    Zeitfaktor dort sinnlos - also genau die Grösse, die den Stau abbildet.

    `neu_planen=False` nimmt den Punkt auf, ohne die Reststrecke neu zu
    rechnen. Gebraucht für nachgereichte Punkte aus einem Funkloch: Ein Plan,
    der ab einer Position von vor zehn Minuten gerechnet wird, ist schon beim
    Erscheinen veraltet - und meldete dazu eine Änderung aufs Telefon. Erst
    der letzte Punkt des Stapels, der die Gegenwart ist, darf umplanen.
    """
    trip = session.trip
    geometry = trip.geometry or []
    profile = trip.energy_profile or []

    km, spacing = point_on_route(geometry, lat, lon)
    plan_value = plan_soc_at(profile, km)

    point = models.LivePoint(lat=lat, lon=lon, soc=soc, speed_kmh=speed_kmh,
                             outside_temp_c=outside_temp_c, km_on_route=km,
                             plan_soc=plan_value, raw_values=raw_values,
                             timestamp=timestamp or datetime.utcnow())
    # Über die Beziehung anhängen und nicht über db.add(): Sonst steht der
    # Punkt zweimal in der geladenen Sammlung - einmal durch das Anhängen,
    # einmal durch die Kaskade - und die Faktoren rechnen mit einem Duplikat.
    session.points.append(point)
    capacity_remember(trip.vehicle, raw_values, point.timestamp)
    db.flush()

    factor = _consumption_factor(session.points)
    if factor is not None:
        session.consumption_factor = factor
    zfaktor = _time_factor(session.points, profile)
    if zfaktor is not None:
        session.time_factor = zfaktor

    # Abweg braucht Dauer, nicht nur Abstand: Eine ungenaue Messung unter
    # einer Brücke ist kein Verlassen der Route.
    if spacing > THRESHOLD_DETOUR_M:
        if session.detour_since is None:
            session.detour_since = point.timestamp
    else:
        session.detour_since = None

    state = _build_state(session, point, spacing)

    # Umgeplant wird mit dem Ladestand, der gilt - gemeldet oder hochgerechnet.
    # Sonst käme ein reiner Positionspunkt mit `None` beim Optimierer an.
    if (new_plan and state.replanning_required
            and _may_new_plan(session, km, state)):
        _replan(db, session, state, km, state.actual_soc)

    session.hint = state.reason
    db.commit()
    return state


# Ab welcher Abweichung ein neuer Kapazitaetswert ueberhaupt geschrieben
# wird. Der Zaehler springt zwischen zwei Messungen um wenige Wattstunden;
# jedes Mal zu schreiben hiesse, bei jedem Messpunkt eine Zeile zu aendern,
# ohne dass sich etwas aendert.
CAPACITY_STEP_KWH = 0.2


def capacity_remember(vehicle, raw_values: dict | None, timestamp) -> None:
    """Die vom Fahrzeug gemeldete Akkukapazitaet am Fahrzeug festhalten.

    Sie kommt als `akku_kwh` in den Rohwerten mit - der Dongle liest sie
    alle vierzig Runden. Aufgehoben wird sie, weil daran **jede**
    Umrechnung zwischen Ladestand und Kilowattstunden haengt: der gemessene
    Verbrauch, der daraus gelernte Korrekturfaktor, die Ladehuebe im
    Ladeplan, die Restreichweite. Im Profil steht eine Prospektangabe fuer
    ein neues Fahrzeug; hier steht, was dieser Akku heute kann.

    Geprueft wird gegen den Profilwert: Mehr als der Prospekt oder weniger
    als die Haelfte ist keine Alterung, sondern ein Lesefehler. Die
    Plausibilitaetsschranke steht bewusst hier und nicht nur im Dongle -
    Messwerte koennen auch von einem Logger kommen, den niemand geprueft
    hat.
    """
    if not vehicle or not raw_values:
        return
    val = raw_values.get("battery_kwh")
    if not isinstance(val, (int, float)):
        return
    if not (0.5 * vehicle.battery_net_kwh <= val
            <= vehicle.battery_net_kwh * 1.05):
        log.info("Kapazitaet %s kWh verworfen - passt nicht zu %s kWh im "
                 "Profil.", val, vehicle.battery_net_kwh)
        return
    so_far = vehicle.measured_capacity_kwh
    if so_far is not None and abs(so_far - val) < CAPACITY_STEP_KWH:
        return
    if so_far is None:
        log.info("Kapazitaet von %s erstmals gemessen: %.1f kWh (Profil %.1f).",
                 vehicle.name, val, vehicle.battery_net_kwh)
    vehicle.measured_capacity_kwh = round(val, 2)
    vehicle.capacity_measured_at = timestamp or datetime.utcnow()


def speed_factor_measured(points: list, energy_profile: list) -> float | None:
    """Wie viel schneller als geplant tatsächlich gefahren wird.

    Das ist der Kehrwert des Zeitfaktors: Wer eine Strecke in 90 % der
    veranschlagten Zeit zurücklegt, fährt elf Prozent schneller. Eine eigene
    Messung braucht es dafür nicht - der Zeitfaktor liegt schon vor, ist um
    Ladepausen bereinigt und gegen Ausreisser abgesichert.

    Gebraucht wird die Zahl, weil das Tempo bisher **geraten** wurde: Der
    Regler in der Planen-Ansicht steht auf 120 %, und niemand weiss, ob das
    stimmt. Über v² ist das der grösste Einzelposten der Prognose.
    """
    factor = _time_factor(points, energy_profile)
    if factor is None or factor <= 0.1:
        return None
    return round(1.0 / factor, 3)


def _replan(db, session: models.LiveSession, state: State, km: float,
              soc: float) -> None:
    profile = session.trip.energy_profile or []

    # Entweder-oder, kein Sowohl-als-auch: Ein aus echten Ladeständen
    # gemessener Verbrauch enthält die Wirkung des Tempos bereits - und noch
    # Beladung, Wetterfehler und Batteriealter dazu. Er ist die bessere
    # Auskunft, sobald es ihn gibt. Solange nicht, ist das gemessene Tempo
    # immer noch weit besser als der Reglerwert von vor der Abfahrt.
    anchor = sum(1 for p in session.points if p.soc is not None)
    velocity = None if anchor >= 2 else speed_factor_measured(session.points, profile)

    try:
        fresh = replanning.schedule(
            db, session.trip, km, soc,
            replanning.read_parameter(session.plan),
            session.consumption_factor, session.time_factor, speed_factor=velocity)
    except Exception as failure:      # noqa: BLE001
        # Eine gescheiterte Umplanung darf die Fahrt nicht beenden: Die
        # Messung läuft weiter, und der alte Plan ist immer noch besser als
        # gar keiner.
        log.warning("Umplanung fehlgeschlagen: %s", failure)
        return

    if not replanning.stops_same(session.plan, fresh):
        state.plan_changed = True
        state.change = replanning.describe_change(session.plan, fresh)
    session.plan = fresh
    state.plan = fresh


def _may_new_plan(session: models.LiveSession, km: float,
                     state: State) -> bool:
    """Sperre gegen einen Plan, der sich im Minutentakt ändert.

    Dringende Gründe - die Säule ist belegt, die Reserve reicht nicht - gehen
    immer durch. Alles andere erst wieder nach `NEUPLANUNG_ABSTAND_KM`: Die
    Abweichung besteht ja weiter, sonst hätte der Auslöser nicht gegriffen.
    Ohne die Sperre rechnete jede einzelne Messung neu.
    """
    if session.plan is None or state.urgent:
        return True
    last_as_of = (session.plan or {}).get("reading_km")
    if last_as_of is None:
        return True
    return (km - last_as_of) >= REPLANNING_SPACING_KM


# ---------------------------------------------------------------------------
# Zustand und Auslöser
# ---------------------------------------------------------------------------

def _build_state(session: models.LiveSession, point: models.LivePoint,
                    spacing_m: float) -> State:
    trip = session.trip
    vehicle = trip.vehicle
    profile = trip.energy_profile or []
    total_km = profile[-1]["km"] if profile else 0.0
    km = point.km_on_route or 0.0
    remaining_km = max(0.0, total_km - km)

    # Der Ladestand, mit dem hier gerechnet wird: der gemeldete, sonst der
    # hochgerechnete. Welcher von beiden es war, steht als eigenes Feld im
    # Zustand - wer am Steuer eine Zahl sieht, soll wissen, ob sie gemessen
    # oder gerechnet ist.
    reported = point.soc is not None
    actual_soc = point.soc if reported else soc_estimate(
        session.points, point, session.consumption_factor)
    soc_source = "gemessen" if reported else "gerechnet"
    if actual_soc is None:
        # Eine Aufzeichnung hat kein Profil, aus dem sich ein Ladestand
        # schätzen liesse. Dann zeigt die Anzeige die letzte Messung dieser
        # Fahrt - als solche gekennzeichnet -, statt leer zu bleiben. Gerechnet
        # wird damit nichts: Ohne Profil gibt es weder Abweichung noch Prognose.
        tail = next((p.soc for p in reversed(session.points)
                       if p.soc is not None), None)
        if tail is not None:
            actual_soc, soc_source = tail, "zuletzt"

    deviation = None
    if point.plan_soc is not None and actual_soc is not None:
        deviation = round(actual_soc - (point.plan_soc + _charged_pp(
            session.points, point)), 2)

    forecast = _forecast_at_target(profile, point, actual_soc,
                                 session.consumption_factor)
    reserve_at = _reserve_at(profile, point, actual_soc,
                               session.consumption_factor, vehicle.reserve_soc)
    shift = _arrival_shift(session, profile, point, total_km)
    upcoming, arrival_soc = _next_stop(session, profile, point, actual_soc)

    required, reason, urgent = _examine_replanning(
        vehicle=vehicle, deviation=deviation, spacing_m=spacing_m,
        detour_since=session.detour_since, now_ts=point.timestamp, forecast=forecast,
        reserve_at=reserve_at, total_km=total_km,
        shift=shift, upcoming=upcoming,
        arrival_soc=arrival_soc)

    return State(
        km_on_route=round(km, 2), spacing_to_route_m=round(spacing_m),
        lat=point.lat, lon=point.lon,
        actual_soc=None if actual_soc is None else round(actual_soc, 2),
        soc_reported=reported, soc_source=soc_source, plan_soc=point.plan_soc,
        deviation_pp=deviation,
        consumption_factor=round(session.consumption_factor, 3),
        time_factor=round(session.time_factor, 3),
        remaining_km=round(remaining_km, 1), forecast_soc_at_target=forecast,
        reserve_at_km=reserve_at, arrival_shift_min=shift,
        next_stop=upcoming, replanning_required=required, reason=reason,
        urgent=urgent)


def _charged_pp(points: list, until_point) -> float:
    """Wie viele Prozentpunkte bis hierher nachgeladen wurden.

    Gebraucht fuer die **Abweichung**, und nur dafuer. Das Energieprofil
    kennt keine Ladestopps: Es rechnet den Ladestand vom Start an
    ununterbrochen herunter und geht auf einer Langstrecke tief ins
    Negative - auf 774 km Hamburg-Muenchen bis auf -257 %. Die Abweichung
    verglich den gemessenen Ladestand direkt damit und meldete nach dem
    ersten Ladestopp dreistellige Prozentpunkte. In einem Probelauf standen
    dort 262 pp; die Kachel "Abweichung" ist damit fuer jede Fahrt mit
    Ladestopp unbrauchbar - also fuer jede lange.

    Wer 40 Punkte nachgeladen hat, soll 40 Punkte ueber dem Profil liegen.
    Genau das rechnet diese Funktion heraus, und uebrig bleibt die Frage,
    um die es geht: Bin ich sparsamer oder durstiger unterwegs als geplant?

    Prognose und Reserve-Marke brauchen das nicht - die rechnen ohnehin mit
    Differenzen ab dem aktuellen Punkt und sind deshalb schon richtig.
    """
    return charge_phases.charged_pp(points, until_point)


def _forecast_at_target(profile: list, point, actual_soc, consumption_factor: float):
    """Der Rest der Strecke mit dem gemessenen Faktor hochgerechnet."""
    if not profile or actual_soc is None:
        return None
    rest_plan = (point.plan_soc or actual_soc) - (profile[-1].get("soc") or 0.0)
    return round(actual_soc - rest_plan * consumption_factor, 2)


def _reserve_at(profile: list, point, actual_soc, consumption_factor: float,
                 reserve_soc: float):
    """Wo die Reserve erreicht wird, wenn es so weitergeht wie bisher."""
    if actual_soc is None:
        return None
    for entry in profile:
        if (entry.get("km") or 0.0) < (point.km_on_route or 0.0):
            continue
        consumed = (point.plan_soc or actual_soc) - (entry.get("soc") or 0.0)
        if actual_soc - consumed * consumption_factor <= reserve_soc:
            return round(entry.get("km") or 0.0, 1)
    return None


def _arrival_shift(session, profile: list, point, total_km: float):
    """Um wie viele Minuten sich die Ankunft verschiebt - Stau inbegriffen.

    Zwei Anteile: was bereits verloren ist, und was der Zeitfaktor auf der
    Reststrecke noch kosten wird. Nur zusammen ergeben sie die Zahl, die
    interessiert.

    Die bereits verbrachte Ladezeit zählt nicht als Verspätung - sie stand so
    im Plan. Siehe `_ladepausen_minuten`.
    """
    points = [p for p in session.points if p.km_on_route is not None]
    if len(points) < 2 or not profile:
        return None

    first = points[0]
    if not first.timestamp or not point.timestamp:
        return None
    actual_minutes = (point.timestamp - first.timestamp).total_seconds() / 60.0
    plan_now = plan_minutes_at(profile, point.km_on_route or 0.0)
    plan_start = plan_minutes_at(profile, first.km_on_route or 0.0)
    plan_target = plan_minutes_at(profile, total_km)
    if plan_now is None or plan_start is None or plan_target is None:
        return None

    so_far = (actual_minutes - (plan_now - plan_start)
              - _charge_pauses_minutes(points, profile))
    rest = max(0.0, plan_target - plan_now) * (session.time_factor - 1.0)
    return round(so_far + rest, 1)


def _next_stop(session, profile: list, point, actual_soc):
    """Der nächste geplante Ladestopp und der dort erwartete Ladestand.

    Der erwartete Wert wird mit dem gemessenen Verbrauchsfaktor hochgerechnet
    und gegen den Plan gehalten. Genau das ist der Auslöser aus dem Konzept:
    nicht die Abweichung hier, sondern die am nächsten Stopp - dort wird sie
    zum Problem.
    """
    stops = ((session.plan or {}).get("stops") or [])
    km = point.km_on_route or 0.0
    upcoming = next((s for s in stops
                      if (s.get("km_on_route") or 0.0) > km + 0.5), None)
    if upcoming is None:
        return None, None

    target_km = upcoming.get("km_on_route") or 0.0
    plan_there = plan_soc_at(profile, target_km)
    if plan_there is None or point.plan_soc is None or actual_soc is None:
        return dict(upcoming), None

    consumed = point.plan_soc - plan_there
    extrapolated = round(actual_soc - consumed * session.consumption_factor, 2)
    description = {"id": upcoming.get("id"), "name": upcoming.get("name"),
                    "km_on_route": target_km,
                    "planned_soc": upcoming.get("arrival_soc"),
                    "expected_soc": extrapolated}
    return description, extrapolated


def _examine_replanning(*, vehicle, deviation, spacing_m, detour_since, now_ts,
                        forecast, reserve_at, total_km, shift,
                        upcoming, arrival_soc) -> tuple[bool, str, bool]:
    """Muss der Plan angefasst werden, warum - und eilt es?

    Die Reihenfolge ist die der Dringlichkeit: Was die Fahrt unmöglich macht,
    steht vor dem, was sie nur unbequem macht. `dringend` entscheidet, ob die
    Sperre gegen zu häufiges Umplanen übergangen wird.
    """
    # 1. Der nächste Ladepunkt ist belegt. Die einzige Verfügbarkeitsangabe,
    #    die wirklich stimmt - und sie macht den Plan sofort wertlos.
    if upcoming and upcoming.get("id") is not None:
        if availability.REPORTS.actual_reported(upcoming["id"]):
            name = upcoming.get("name") or "Der nächste Ladepunkt"
            return True, f"{name} ist als belegt gemeldet - Ausweichen.", True

    # 2. Es reicht nicht bis zum Ziel.
    if reserve_at is not None and reserve_at < total_km:
        return True, (f"Reserve wird bei km {reserve_at:.0f} erreicht - "
                      f"vorher laden."), True
    if forecast is not None and forecast < vehicle.reserve_soc:
        return True, (f"Ankunft mit {forecast:.0f} % prognostiziert, "
                      f"unter der Reserve von {vehicle.reserve_soc:.0f} %."), True

    # 3. Abseits der Route - aber erst, wenn es anhält.
    if detour_since is not None and now_ts is not None:
        duration = (now_ts - detour_since).total_seconds()
        if duration >= THRESHOLD_DETOUR_S:
            return True, (f"Seit {duration / 60:.0f} min mehr als "
                          f"{spacing_m:.0f} m neben der Route."), True

    # 4. Am nächsten Stopp kommt etwas anderes an als geplant.
    if upcoming and arrival_soc is not None:
        planned = upcoming.get("planned_soc")
        if planned is not None and abs(arrival_soc - planned) >= THRESHOLD_SOC_PP:
            name = upcoming.get("name") or "nächster Stopp"
            return True, (f"Ankunft an {name} mit {arrival_soc:.0f} % statt "
                          f"{planned:.0f} % - Ladestopps neu rechnen."), False

    # 5. Ohne Plan bleibt die Abweichung hier die beste verfügbare Aussage.
    if not upcoming and deviation is not None and abs(deviation) >= THRESHOLD_SOC_PP:
        direction = "unter" if deviation < 0 else "über"
        return True, (f"{abs(deviation):.0f} Prozentpunkte {direction} Plan - "
                      f"Ladestopps neu rechnen."), False

    # 6. Stau: Der Verbrauch merkt ihn kaum, die Ankunftszeit sehr wohl.
    if shift is not None and abs(shift) >= THRESHOLD_ARRIVAL_MIN:
        word = "später" if shift > 0 else "früher"
        return True, (f"Ankunft {abs(shift):.0f} min {word} als "
                      f"geplant."), False

    if deviation is not None and abs(deviation) >= THRESHOLD_SOC_PP / 2:
        return False, f"{deviation:+.0f} Prozentpunkte gegenüber Plan.", False
    return False, "im Plan", False


def state_as_dict(state: State) -> dict:
    records = asdict(state)
    records.pop("urgent", None)      # nur für die interne Sperre
    return records
