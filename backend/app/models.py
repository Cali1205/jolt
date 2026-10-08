"""Datenmodell von jolt.

Die Aufteilung folgt der Frage, was sich wie oft ändert: Fahrzeugparameter
selten, Ladekurven gelegentlich, Ladepunkte bei jedem Import, Live-Messpunkte
im Sekundentakt.
"""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import relationship

from .database import Base


class AuthSession(Base):
    """Angemeldetes Gerät. Ein Token je Gerät, damit sich eines einzeln
    abmelden lässt, ohne die anderen mitzunehmen."""
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    device = Column(String(120), default="")
    created = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)


class PushSubscription(Base):
    """Ein Gerät, das Benachrichtigungen bekommen will.

    Der `endpoint` ist die vom Browser vergebene Adresse beim Push-Dienst und
    zugleich der Schlüssel: Derselbe Browser liefert ihn erneut, solange die
    Erlaubnis besteht. Deshalb ist er eindeutig - ein zweites Abo desselben
    Geräts hiesse, dass dasselbe Telefon jede Meldung doppelt bekommt.

    Die beiden Schlüssel gehören dem Gerät, nicht dem Server: Mit ihnen wird
    die Nutzlast so verschlüsselt, dass der Push-Dienst sie weiterreicht, ohne
    sie lesen zu können.
    """
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True)
    endpoint = Column(String(500), unique=True, nullable=False, index=True)
    p256dh = Column(String(200), nullable=False)
    auth = Column(String(100), nullable=False)
    device = Column(String(120), default="")

    # Aufeinanderfolgende Fehlversuche. Ein totes Abo wird sofort gelöscht
    # (404/410); dieser Zähler zeigt nur, dass ein Gerät dauerhaft nicht
    # erreichbar ist, ohne sich abgemeldet zu haben.
    failure = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Vehicle(Base):
    """Alles, was das Verbrauchsmodell über das Auto wissen muss.

    Die Parameter sind absichtlich physikalisch und nicht "kWh/100 km": Nur so
    lässt sich beantworten, was 130 statt 110 km/h kosten oder was ein Pass
    verbraucht. Der pauschale Wert kann das nicht - siehe konzept-routenplaner.md.
    """
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)

    battery_gross_kwh = Column(Float, nullable=False)
    # Nutzbar ist immer weniger als brutto - der Puffer oben und unten gehört
    # dem Batteriemanagement. Gerechnet wird ausschliesslich mit netto.
    battery_net_kwh = Column(Float, nullable=False)

    curb_mass_kg = Column(Float, nullable=False, default=1800.0)
    payload_kg = Column(Float, nullable=False, default=150.0)

    c_w = Column(Float, nullable=False, default=0.28)
    frontal_area_m2 = Column(Float, nullable=False, default=2.3)
    c_rr = Column(Float, nullable=False, default=0.010)

    # Höchstgeschwindigkeit des Fahrzeugs in km/h. NULL = keine Grenze im
    # Modell. Der Tempo-Regler der Planung stösst daran an, statt das
    # Modell mit Geschwindigkeiten rechnen zu lassen, die das Auto nicht fährt.
    max_speed_kmh = Column(Float)

    eta_drive = Column(Float, nullable=False, default=0.88)
    # Rekuperation holt nie alles zurück. Deshalb ist die Bilanz über einen
    # Pass negativ, obwohl man am Ende wieder auf Ausgangshöhe steht.
    eta_regen = Column(Float, nullable=False, default=0.70)

    # Grundlast unabhängig von der Heizung: Steuergeräte, Licht, Pumpen.
    p_aux_w = Column(Float, nullable=False, default=350.0)
    # Der grösste Einzelunterschied im Winter: Eine Wärmepumpe braucht für
    # dieselbe Kabinentemperatur grob die Hälfte eines elektrischen Heizers.
    # Bei -5 °C sind das rund 1,8 kW Unterschied - über vier Stunden Fahrt
    # mehr als 7 kWh, also der Grund für einen zusätzlichen Ladestopp.
    heat_pump = Column(Boolean, nullable=False, default=True)

    reserve_soc = Column(Float, nullable=False, default=10.0)
    target_soc = Column(Float, nullable=False, default=20.0)

    max_charge_power_kw = Column(Float, nullable=False, default=150.0)
    connector_type = Column(String(20), nullable=False, default="CCS")

    # Namen (oder Teile davon, z.B. "EnBW"), die der Optimierer bei der
    # Stoppwahl bevorzugt - siehe laden/availability.py:betreiber_bonus().
    # Kein harter Filter: ein nicht bevorzugter Anbieter bleibt wählbar, wird
    # nur nicht zusätzlich begünstigt.
    preferred_operators = Column(JSON, nullable=False, default=list)

    # Was eine Kilowattstunde kostet - am Fahrzeug, weil sie am Vertrag
    # hängt und nicht an der Säule. `strompreise` ist eine Liste von
    # {muster, eur_kwh}, `strompreis_eur_kwh` gilt für alles Übrige.
    # Siehe laden/prices.py.
    electricity_price_eur_kwh = Column(Float, nullable=False, default=0.59)
    electricity_prices = Column(JSON)

    # Aus echten Fahrten gelernt (energie/calibration.py). 1.0 = ungeprüft.
    correction_factor = Column(Float, nullable=False, default=1.0)
    # Was das Fahrzeug selbst ueber seine Kapazitaet sagt (DID 222AB2),
    # und wann. NULL heisst "nie gemessen" - siehe Migration 0014.
    measured_capacity_kwh = Column(Float)
    capacity_measured_at = Column(DateTime)

    # Langlebiges Geheimnis für einen Logger im Auto - OBD2-Dongle, Kurzbefehl,
    # was auch immer. Er kann die ID der laufenden Live-Sitzung nicht kennen:
    # Die entsteht erst beim Losfahren in der App und wechselt mit jeder Fahrt.
    # Ein Gerät, das im Auto verbaut ist und beim Anschalten einfach zu senden
    # beginnt, braucht deshalb einen Schlüssel, der bleibt - das Backend sucht
    # sich die laufende Sitzung dieses Fahrzeugs dann selbst.
    # NULL heisst "kein Logger eingerichtet".
    logger_token = Column(String(64), unique=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def capacity_kwh(self) -> float:
        """Die Kapazitaet, mit der gerechnet wird - gemessen vor Prospekt.

        Der Wert im Profil ist die Angabe des Herstellers fuer ein neues
        Fahrzeug. Meldet das Auto selbst eine Zahl, ist sie besser: Sie
        beschreibt **diesen** Akku in **diesem** Zustand. Beim ID.Buzz nach
        knapp 60 000 km sind das 73,8 statt 77 kWh - vier Prozent, die
        sonst durchgaengig in dieselbe Richtung falsch liegen.

        Die Schranke faengt eine unsinnige Messung ab: Ein Wert ueber dem
        Prospektwert oder unter der Haelfte davon ist keine Alterung,
        sondern ein Lesefehler, und dann gilt das Profil.
        """
        measured = self.measured_capacity_kwh
        if measured and 0.5 * self.battery_net_kwh <= measured <= self.battery_net_kwh * 1.05:
            return measured
        return self.battery_net_kwh

    charge_curve = relationship("ChargeCurvePoint", back_populates="vehicle",
                             cascade="all, delete-orphan",
                             order_by="ChargeCurvePoint.soc_percent")

    @property
    def mass_kg(self) -> float:
        return self.curb_mass_kg + self.payload_kg


class ChargeCurvePoint(Base):
    """Stützstelle der Ladekurve: bei diesem SoC diese Leistung.

    Eigene Tabelle statt eines JSON-Feldes am Fahrzeug, weil die Kurve das ist,
    was man nach den ersten echten Ladevorgängen nachschärft - unabhängig von
    allen anderen Fahrzeugdaten.
    """
    __tablename__ = "charge_curve_points"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    soc_percent = Column(Float, nullable=False)
    kw = Column(Float, nullable=False)

    vehicle = relationship("Vehicle", back_populates="charge_curve")

    __table_args__ = (UniqueConstraint("vehicle_id", "soc_percent",
                                       name="uq_ladekurve_soc"),)


class ChargePoint(Base):
    """Ein Ladestandort aus einer der Importquellen.

    `anschluesse` ist bewusst JSON: Die Quellen liefern unterschiedlich viele
    Stecker mit unterschiedlichen Leistungen, und daraus je ein eigenes Objekt
    zu machen brächte nichts - gefiltert wird über `max_kw` und `steckertypen`,
    beide beim Import mitgeschrieben.
    """
    __tablename__ = "charge_points"

    id = Column(Integer, primary_key=True)
    source = Column(String(20), nullable=False)      # "bnetza" | "ocm"
    foreign_id = Column(String(80), nullable=False)

    name = Column(String(200), default="")
    operator = Column(String(200), default="")
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    address = Column(String(250), default="")
    # OCM liefert bei Standorten, die mehrere Postleitzahlen abdecken (grosse
    # Einkaufszentren etwa), eine Semikolon-Liste statt einer einzelnen PLZ -
    # "33000;33100;33200;33300;33800" ist 29 Zeichen lang und hat den
    # OCM-Import an einem realen Datensatz ("Auchan Bordeaux Lac") abgebrochen.
    postcode = Column(String(40), default="")
    city = Column(String(120), default="")
    country = Column(String(2), default="DE")

    connectors = Column(JSON, default=list)
    # Denormalisiert, damit die Korridor-Abfrage ohne JSON-Auswertung filtern
    # kann - JSON-Zugriffe unterscheiden sich zwischen SQLite und Postgres.
    max_kw = Column(Float, nullable=False, default=0.0)
    point_count = Column(Integer, nullable=False, default=1)
    connector_types = Column(String(120), default="")   # "CCS,Typ2"

    as_of = Column(String(20), default="")

    # Was einen Ladepunkt für eine konkrete Fahrt unbrauchbar macht, steht
    # bei den Quellen in Worten - und wurde bisher weggeworfen. Siehe
    # Migration 0013.
    #
    # NULL heisst bei den Wahrheitswerten **unbekannt** und nicht "nein":
    # Für den grössten Teil der Datenbank gibt es die Angabe nicht, und wer
    # Unbekanntes wie Ausgeschlossenes behandelt, verliert fast alles.
    operational = Column(Boolean)
    access = Column(String(60))
    membership_required = Column(Boolean)
    # Freitext der Quelle: Kosten, Zugangshinweise, Kommentare. Wird
    # zunächst nur aufgehoben - siehe Migration 0013.
    hints = Column(JSON)

    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "foreign_id", name="uq_ladepunkt_quelle"),
        # Der Korridor fragt immer über ein Rechteck ab: erst lat, dann lon.
        Index("ix_ladepunkt_pos", "lat", "lon"),
        Index("ix_ladepunkt_kw", "max_kw"),
    )


class Trip(Base):
    """Eine geplante Fahrt samt gerechnetem Energieprofil."""
    __tablename__ = "trips"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False)

    start_text = Column(String(250), default="")
    start_lat = Column(Float, nullable=False)
    start_lon = Column(Float, nullable=False)
    target_text = Column(String(250), default="")
    target_lat = Column(Float, nullable=False)
    target_lon = Column(Float, nullable=False)

    start_soc = Column(Float, nullable=False)
    speed_factor = Column(Float, nullable=False, default=1.0)
    outside_temp_c = Column(Float)
    # Zuladung dieser Fahrt. NULL heisst "es galt das Fahrzeugprofil" - so
    # bleiben Fahrten aus der Zeit vor diesem Feld korrekt lesbar, statt
    # rückwirkend eine Zuladung von 0 kg zu behaupten.
    payload_kg = Column(Float)

    # Zuschlag auf den Luftwiderstand für alles, was aussen dranhängt -
    # Fahrradträger, Dachbox. 1.0 heisst "nichts dran". Gehört zur Fahrt und
    # nicht zum Fahrzeug: Dieselbe Strecke einmal mit und einmal ohne Träger
    # sind zwei verschiedene Energiebilanzen, und der Träger ist im Sommer
    # dran und im Winter nicht.
    air_drag_factor = Column(Float, nullable=False, default=1.0)

    # Ein Anhänger gehört zur Fahrt: Masse in kg und zusätzliche
    # Luftwiderstandsfläche (c_w mal A) in m². NULL heisst "keiner".
    # Bewusst nicht im `luftwiderstand_faktor` aufgegangen: Ein Wohnwagen
    # verdoppelt nicht den cw-Wert des Autos, er bringt eine eigene Fläche mit
    # und 1,3 t dazu.
    trailer_kg = Column(Float)
    trailer_cwa_m2 = Column(Float)
    # Höchstgeschwindigkeit dieser Fahrt in km/h - für ein Gespann 100, als
    # harte Grenze und nicht als Vorliebe. NULL = keine über die des
    # Fahrzeugs hinaus.
    speed_max_kmh = Column(Float)

    # Aufgezeichnet statt geplant: Geometrie und Energieprofil sind dann zu
    # Beginn leer und entstehen beim Beenden aus den Messpunkten. Siehe
    # live/recording.py.
    recording = Column(Boolean, nullable=False, default=False)

    distance_m = Column(Float, default=0.0)
    drive_time_s = Column(Float, default=0.0)

    # Anzeige-Geometrie, auf ~1 Punkt je 250 m ausgedünnt. Die volle
    # ORS-Antwort hat auf einer Langstrecke fünfstellig viele Stützpunkte;
    # die brauchen weder die Karte noch die Prognose.
    geometry = Column(JSON, default=list)          # [[lon, lat, hoehe], ...]
    energy_profile = Column(JSON, default=list)      # [{"km","soc","kwh"}, ...]

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    vehicle = relationship("Vehicle")
    # Damit die Historie "geplant" von "tatsächlich gefahren" unterscheiden
    # kann, und das Löschen einer Fahrt ihre Sitzungen mitnimmt statt an der
    # Fremdschlüsselbedingung zu scheitern.
    live_sessions = relationship("LiveSession", back_populates="trip",
                                  cascade="all, delete-orphan")


class LiveSession(Base):
    """Eine laufende Fahrt, in die Messpunkte hereinkommen.

    Die Quelle der Messpunkte ist bewusst offen: heute die PWA oder der
    Simulator, später der OBD2-Logger oder eine Hersteller-API. Am Schema
    ändert das nichts - nur daran, wer POSTet.
    """
    __tablename__ = "live_sessions"

    id = Column(Integer, primary_key=True)
    trip_id = Column(Integer, ForeignKey("trips.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at = Column(DateTime)
    running = Column(Boolean, default=True, nullable=False)

    # Laufender Verbrauchsfaktor: Ist geteilt durch Soll über die letzten
    # Kilometer. > 1 heisst "verbraucht mehr als gerechnet".
    consumption_factor = Column(Float, default=1.0, nullable=False)
    # Dasselbe für die Zeit. Der Verbrauchsfaktor allein sieht einen Stau
    # nicht: Wer im Stau steht, verbraucht je Kilometer sogar mehr, aber die
    # Ankunftszeit verschiebt sich um ein Vielfaches davon. Für den Auslöser
    # "Ankunftszeit verschiebt sich" braucht es deshalb eine eigene Zahl.
    time_factor = Column(Float, default=1.0, nullable=False)
    hint = Column(Text, default="")

    # Der aktuell gültige Ladeplan. Beim Start der Fahrt gerechnet und
    # unterwegs ersetzt, sobald ein Auslöser greift. Er liegt hier und nicht
    # an der Fahrt, weil er zur *laufenden* Fahrt gehört: Dieselbe geplante
    # Strecke ein zweites Mal gefahren ergibt einen anderen Plan.
    plan = Column(JSON)
    # Seit wann das Fahrzeug neben der Route ist. Die Schwelle ist "mehr als
    # 500 m für mehr als eine Minute" - ohne diesen Zeitstempel wäre jede
    # ungenaue GPS-Messung an einer Brücke eine Neuplanung.
    detour_since = Column(DateTime)

    trip = relationship("Trip", back_populates="live_sessions")
    points = relationship("LivePoint", back_populates="session",
                          cascade="all, delete-orphan",
                          order_by="LivePoint.timestamp")


class LivePoint(Base):
    __tablename__ = "live_points"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("live_sessions.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    # NULL heisst "Position gemeldet, Ladestand nicht bekannt". Die beiden
    # Grössen haben verschiedene Taktraten: Das Telefon liefert die Position
    # im Sekundentakt und umsonst, den Ladestand tippt jemand ein, wenn er
    # ohnehin an der Säule steht. Was dazwischen gilt, rechnet
    # `live/session.py` aus dem Energieprofil hoch.
    soc = Column(Float)
    # Rohmessung, absichtlich nicht die Grundlage des Tempofaktors. Der wird
    # aus Strecke und Zeit über ein Fenster von Kilometern gebildet
    # (`live/session.py`), und zwar aus zwei Gründen: Die Momentangeschwindig-
    # keit des GPS ist verrauscht, und auf iOS liefert `coords.speed`
    # regelmässig gar nichts. Ein Faktor, der auf einem Feld beruht, das je
    # nach Telefon fehlt, wäre kein Faktor.
    #
    # Aufgehoben wird sie trotzdem: Sie kostet vier Byte je Messpunkt und ist
    # das einzige, woran sich später nachprüfen liesse, ob die Rechnung aus
    # Strecke und Zeit mit dem übereinstimmt, was der Tacho sah.
    speed_kmh = Column(Float)
    outside_temp_c = Column(Float)

    # Alles, was die Quelle sonst noch mitgeschickt hat - Packspannung,
    # Strom, Kilometerstand, der unverrechnete Rohwert des Ladestands.
    # Gerechnet wird damit nicht; es liegt hier, damit sich später auswerten
    # lässt, was sich sonst nur durch eine zweite Fahrt klären liesse.
    raw_values = Column(JSON)

    # Beim Eintreffen berechnet und mitgeschrieben, damit die Auswertung
    # später nicht die ganze Route erneut projizieren muss.
    km_on_route = Column(Float)
    plan_soc = Column(Float)

    session = relationship("LiveSession", back_populates="points")
