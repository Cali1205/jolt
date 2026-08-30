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


class Sitzung(Base):
    """Angemeldetes Gerät. Ein Token je Gerät, damit sich eines einzeln
    abmelden lässt, ohne die anderen mitzunehmen."""
    __tablename__ = "sitzungen"

    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    geraet = Column(String(120), default="")
    erstellt = Column(DateTime, default=datetime.utcnow, nullable=False)
    zuletzt_gesehen = Column(DateTime, default=datetime.utcnow, nullable=False)


class PushAbo(Base):
    """Ein Gerät, das Benachrichtigungen bekommen will.

    Der `endpoint` ist die vom Browser vergebene Adresse beim Push-Dienst und
    zugleich der Schlüssel: Derselbe Browser liefert ihn erneut, solange die
    Erlaubnis besteht. Deshalb ist er eindeutig - ein zweites Abo desselben
    Geräts hiesse, dass dasselbe Telefon jede Meldung doppelt bekommt.

    Die beiden Schlüssel gehören dem Gerät, nicht dem Server: Mit ihnen wird
    die Nutzlast so verschlüsselt, dass der Push-Dienst sie weiterreicht, ohne
    sie lesen zu können.
    """
    __tablename__ = "push_abos"

    id = Column(Integer, primary_key=True)
    endpoint = Column(String(500), unique=True, nullable=False, index=True)
    p256dh = Column(String(200), nullable=False)
    auth = Column(String(100), nullable=False)
    geraet = Column(String(120), default="")

    # Aufeinanderfolgende Fehlversuche. Ein totes Abo wird sofort gelöscht
    # (404/410); dieser Zähler zeigt nur, dass ein Gerät dauerhaft nicht
    # erreichbar ist, ohne sich abgemeldet zu haben.
    fehler = Column(Integer, default=0, nullable=False)
    angelegt = Column(DateTime, default=datetime.utcnow, nullable=False)


class Fahrzeug(Base):
    """Alles, was das Verbrauchsmodell über das Auto wissen muss.

    Die Parameter sind absichtlich physikalisch und nicht "kWh/100 km": Nur so
    lässt sich beantworten, was 130 statt 110 km/h kosten oder was ein Pass
    verbraucht. Der pauschale Wert kann das nicht - siehe konzept-routenplaner.md.
    """
    __tablename__ = "fahrzeuge"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)

    akku_brutto_kwh = Column(Float, nullable=False)
    # Nutzbar ist immer weniger als brutto - der Puffer oben und unten gehört
    # dem Batteriemanagement. Gerechnet wird ausschliesslich mit netto.
    akku_netto_kwh = Column(Float, nullable=False)

    leermasse_kg = Column(Float, nullable=False, default=1800.0)
    zuladung_kg = Column(Float, nullable=False, default=150.0)

    c_w = Column(Float, nullable=False, default=0.28)
    stirnflaeche_m2 = Column(Float, nullable=False, default=2.3)
    c_rr = Column(Float, nullable=False, default=0.010)

    eta_antrieb = Column(Float, nullable=False, default=0.88)
    # Rekuperation holt nie alles zurück. Deshalb ist die Bilanz über einen
    # Pass negativ, obwohl man am Ende wieder auf Ausgangshöhe steht.
    eta_rekup = Column(Float, nullable=False, default=0.70)

    # Grundlast unabhängig von der Heizung: Steuergeräte, Licht, Pumpen.
    p_neben_w = Column(Float, nullable=False, default=350.0)
    # Der grösste Einzelunterschied im Winter: Eine Wärmepumpe braucht für
    # dieselbe Kabinentemperatur grob die Hälfte eines elektrischen Heizers.
    # Bei -5 °C sind das rund 1,8 kW Unterschied - über vier Stunden Fahrt
    # mehr als 7 kWh, also der Grund für einen zusätzlichen Ladestopp.
    waermepumpe = Column(Boolean, nullable=False, default=True)

    reserve_soc = Column(Float, nullable=False, default=10.0)
    ziel_soc = Column(Float, nullable=False, default=20.0)

    max_ladeleistung_kw = Column(Float, nullable=False, default=150.0)
    steckertyp = Column(String(20), nullable=False, default="CCS")

    # Namen (oder Teile davon, z.B. "EnBW"), die der Optimierer bei der
    # Stoppwahl bevorzugt - siehe laden/verfuegbarkeit.py:betreiber_bonus().
    # Kein harter Filter: ein nicht bevorzugter Anbieter bleibt wählbar, wird
    # nur nicht zusätzlich begünstigt.
    bevorzugte_betreiber = Column(JSON, nullable=False, default=list)

    # Was eine Kilowattstunde kostet - am Fahrzeug, weil sie am Vertrag
    # hängt und nicht an der Säule. `strompreise` ist eine Liste von
    # {muster, eur_kwh}, `strompreis_eur_kwh` gilt für alles Übrige.
    # Siehe laden/preise.py.
    strompreis_eur_kwh = Column(Float, nullable=False, default=0.59)
    strompreise = Column(JSON)

    # Aus echten Fahrten gelernt (energie/kalibrierung.py). 1.0 = ungeprüft.
    korrekturfaktor = Column(Float, nullable=False, default=1.0)
    # Was das Fahrzeug selbst ueber seine Kapazitaet sagt (DID 222AB2),
    # und wann. NULL heisst "nie gemessen" - siehe Migration 0014.
    gemessene_kapazitaet_kwh = Column(Float)
    kapazitaet_gemessen_am = Column(DateTime)

    # Langlebiges Geheimnis für einen Logger im Auto - OBD2-Dongle, Kurzbefehl,
    # was auch immer. Er kann die ID der laufenden Live-Sitzung nicht kennen:
    # Die entsteht erst beim Losfahren in der App und wechselt mit jeder Fahrt.
    # Ein Gerät, das im Auto verbaut ist und beim Anschalten einfach zu senden
    # beginnt, braucht deshalb einen Schlüssel, der bleibt - das Backend sucht
    # sich die laufende Sitzung dieses Fahrzeugs dann selbst.
    # NULL heisst "kein Logger eingerichtet".
    logger_token = Column(String(64), unique=True, index=True)

    angelegt = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def kapazitaet_kwh(self) -> float:
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
        gemessen = self.gemessene_kapazitaet_kwh
        if gemessen and 0.5 * self.akku_netto_kwh <= gemessen <= self.akku_netto_kwh * 1.05:
            return gemessen
        return self.akku_netto_kwh

    ladekurve = relationship("Ladekurvenpunkt", back_populates="fahrzeug",
                             cascade="all, delete-orphan",
                             order_by="Ladekurvenpunkt.soc_prozent")

    @property
    def masse_kg(self) -> float:
        return self.leermasse_kg + self.zuladung_kg


class Ladekurvenpunkt(Base):
    """Stützstelle der Ladekurve: bei diesem SoC diese Leistung.

    Eigene Tabelle statt eines JSON-Feldes am Fahrzeug, weil die Kurve das ist,
    was man nach den ersten echten Ladevorgängen nachschärft - unabhängig von
    allen anderen Fahrzeugdaten.
    """
    __tablename__ = "ladekurvenpunkte"

    id = Column(Integer, primary_key=True)
    fahrzeug_id = Column(Integer, ForeignKey("fahrzeuge.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    soc_prozent = Column(Float, nullable=False)
    kw = Column(Float, nullable=False)

    fahrzeug = relationship("Fahrzeug", back_populates="ladekurve")

    __table_args__ = (UniqueConstraint("fahrzeug_id", "soc_prozent",
                                       name="uq_ladekurve_soc"),)


class Ladepunkt(Base):
    """Ein Ladestandort aus einer der Importquellen.

    `anschluesse` ist bewusst JSON: Die Quellen liefern unterschiedlich viele
    Stecker mit unterschiedlichen Leistungen, und daraus je ein eigenes Objekt
    zu machen brächte nichts - gefiltert wird über `max_kw` und `steckertypen`,
    beide beim Import mitgeschrieben.
    """
    __tablename__ = "ladepunkte"

    id = Column(Integer, primary_key=True)
    quelle = Column(String(20), nullable=False)      # "bnetza" | "ocm"
    fremd_id = Column(String(80), nullable=False)

    name = Column(String(200), default="")
    betreiber = Column(String(200), default="")
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    adresse = Column(String(250), default="")
    # OCM liefert bei Standorten, die mehrere Postleitzahlen abdecken (grosse
    # Einkaufszentren etwa), eine Semikolon-Liste statt einer einzelnen PLZ -
    # "33000;33100;33200;33300;33800" ist 29 Zeichen lang und hat den
    # OCM-Import an einem realen Datensatz ("Auchan Bordeaux Lac") abgebrochen.
    plz = Column(String(40), default="")
    ort = Column(String(120), default="")
    land = Column(String(2), default="DE")

    anschluesse = Column(JSON, default=list)
    # Denormalisiert, damit die Korridor-Abfrage ohne JSON-Auswertung filtern
    # kann - JSON-Zugriffe unterscheiden sich zwischen SQLite und Postgres.
    max_kw = Column(Float, nullable=False, default=0.0)
    anzahl_punkte = Column(Integer, nullable=False, default=1)
    steckertypen = Column(String(120), default="")   # "CCS,Typ2"

    stand = Column(String(20), default="")

    # Was einen Ladepunkt für eine konkrete Fahrt unbrauchbar macht, steht
    # bei den Quellen in Worten - und wurde bisher weggeworfen. Siehe
    # Migration 0013.
    #
    # NULL heisst bei den Wahrheitswerten **unbekannt** und nicht "nein":
    # Für den grössten Teil der Datenbank gibt es die Angabe nicht, und wer
    # Unbekanntes wie Ausgeschlossenes behandelt, verliert fast alles.
    betriebsbereit = Column(Boolean)
    zugang = Column(String(60))
    mitgliedschaft_noetig = Column(Boolean)
    # Freitext der Quelle: Kosten, Zugangshinweise, Kommentare. Wird
    # zunächst nur aufgehoben - siehe Migration 0013.
    hinweise = Column(JSON)

    aktualisiert = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("quelle", "fremd_id", name="uq_ladepunkt_quelle"),
        # Der Korridor fragt immer über ein Rechteck ab: erst lat, dann lon.
        Index("ix_ladepunkt_pos", "lat", "lon"),
        Index("ix_ladepunkt_kw", "max_kw"),
    )


class Fahrt(Base):
    """Eine geplante Fahrt samt gerechnetem Energieprofil."""
    __tablename__ = "fahrten"

    id = Column(Integer, primary_key=True)
    fahrzeug_id = Column(Integer, ForeignKey("fahrzeuge.id"), nullable=False)

    start_text = Column(String(250), default="")
    start_lat = Column(Float, nullable=False)
    start_lon = Column(Float, nullable=False)
    ziel_text = Column(String(250), default="")
    ziel_lat = Column(Float, nullable=False)
    ziel_lon = Column(Float, nullable=False)

    start_soc = Column(Float, nullable=False)
    tempo_faktor = Column(Float, nullable=False, default=1.0)
    aussentemp_c = Column(Float)
    # Zuladung dieser Fahrt. NULL heisst "es galt das Fahrzeugprofil" - so
    # bleiben Fahrten aus der Zeit vor diesem Feld korrekt lesbar, statt
    # rückwirkend eine Zuladung von 0 kg zu behaupten.
    zuladung_kg = Column(Float)

    # Zuschlag auf den Luftwiderstand für alles, was aussen dranhängt -
    # Fahrradträger, Dachbox. 1.0 heisst "nichts dran". Gehört zur Fahrt und
    # nicht zum Fahrzeug: Dieselbe Strecke einmal mit und einmal ohne Träger
    # sind zwei verschiedene Energiebilanzen, und der Träger ist im Sommer
    # dran und im Winter nicht.
    luftwiderstand_faktor = Column(Float, nullable=False, default=1.0)

    # Aufgezeichnet statt geplant: Geometrie und Energieprofil sind dann zu
    # Beginn leer und entstehen beim Beenden aus den Messpunkten. Siehe
    # live/aufzeichnung.py.
    aufzeichnung = Column(Boolean, nullable=False, default=False)

    strecke_m = Column(Float, default=0.0)
    fahrzeit_s = Column(Float, default=0.0)

    # Anzeige-Geometrie, auf ~1 Punkt je 250 m ausgedünnt. Die volle
    # ORS-Antwort hat auf einer Langstrecke fünfstellig viele Stützpunkte;
    # die brauchen weder die Karte noch die Prognose.
    geometrie = Column(JSON, default=list)          # [[lon, lat, hoehe], ...]
    energieprofil = Column(JSON, default=list)      # [{"km","soc","kwh"}, ...]

    angelegt = Column(DateTime, default=datetime.utcnow, nullable=False)

    fahrzeug = relationship("Fahrzeug")
    # Damit die Historie "geplant" von "tatsächlich gefahren" unterscheiden
    # kann, und das Löschen einer Fahrt ihre Sitzungen mitnimmt statt an der
    # Fremdschlüsselbedingung zu scheitern.
    live_sitzungen = relationship("LiveSitzung", back_populates="fahrt",
                                  cascade="all, delete-orphan")


class LiveSitzung(Base):
    """Eine laufende Fahrt, in die Messpunkte hereinkommen.

    Die Quelle der Messpunkte ist bewusst offen: heute die PWA oder der
    Simulator, später der OBD2-Logger oder eine Hersteller-API. Am Schema
    ändert das nichts - nur daran, wer POSTet.
    """
    __tablename__ = "live_sitzungen"

    id = Column(Integer, primary_key=True)
    fahrt_id = Column(Integer, ForeignKey("fahrten.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    gestartet = Column(DateTime, default=datetime.utcnow, nullable=False)
    beendet = Column(DateTime)
    laeuft = Column(Boolean, default=True, nullable=False)

    # Laufender Verbrauchsfaktor: Ist geteilt durch Soll über die letzten
    # Kilometer. > 1 heisst "verbraucht mehr als gerechnet".
    verbrauchsfaktor = Column(Float, default=1.0, nullable=False)
    # Dasselbe für die Zeit. Der Verbrauchsfaktor allein sieht einen Stau
    # nicht: Wer im Stau steht, verbraucht je Kilometer sogar mehr, aber die
    # Ankunftszeit verschiebt sich um ein Vielfaches davon. Für den Auslöser
    # "Ankunftszeit verschiebt sich" braucht es deshalb eine eigene Zahl.
    zeitfaktor = Column(Float, default=1.0, nullable=False)
    hinweis = Column(Text, default="")

    # Der aktuell gültige Ladeplan. Beim Start der Fahrt gerechnet und
    # unterwegs ersetzt, sobald ein Auslöser greift. Er liegt hier und nicht
    # an der Fahrt, weil er zur *laufenden* Fahrt gehört: Dieselbe geplante
    # Strecke ein zweites Mal gefahren ergibt einen anderen Plan.
    plan = Column(JSON)
    # Seit wann das Fahrzeug neben der Route ist. Die Schwelle ist "mehr als
    # 500 m für mehr als eine Minute" - ohne diesen Zeitstempel wäre jede
    # ungenaue GPS-Messung an einer Brücke eine Neuplanung.
    abweg_seit = Column(DateTime)

    fahrt = relationship("Fahrt", back_populates="live_sitzungen")
    punkte = relationship("LivePunkt", back_populates="sitzung",
                          cascade="all, delete-orphan",
                          order_by="LivePunkt.zeit")


class LivePunkt(Base):
    __tablename__ = "live_punkte"

    id = Column(Integer, primary_key=True)
    sitzung_id = Column(Integer, ForeignKey("live_sitzungen.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    zeit = Column(DateTime, default=datetime.utcnow, nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    # NULL heisst "Position gemeldet, Ladestand nicht bekannt". Die beiden
    # Grössen haben verschiedene Taktraten: Das Telefon liefert die Position
    # im Sekundentakt und umsonst, den Ladestand tippt jemand ein, wenn er
    # ohnehin an der Säule steht. Was dazwischen gilt, rechnet
    # `live/sitzung.py` aus dem Energieprofil hoch.
    soc = Column(Float)
    # Rohmessung, absichtlich nicht die Grundlage des Tempofaktors. Der wird
    # aus Strecke und Zeit über ein Fenster von Kilometern gebildet
    # (`live/sitzung.py`), und zwar aus zwei Gründen: Die Momentangeschwindig-
    # keit des GPS ist verrauscht, und auf iOS liefert `coords.speed`
    # regelmässig gar nichts. Ein Faktor, der auf einem Feld beruht, das je
    # nach Telefon fehlt, wäre kein Faktor.
    #
    # Aufgehoben wird sie trotzdem: Sie kostet vier Byte je Messpunkt und ist
    # das einzige, woran sich später nachprüfen liesse, ob die Rechnung aus
    # Strecke und Zeit mit dem übereinstimmt, was der Tacho sah.
    tempo_kmh = Column(Float)
    aussentemp_c = Column(Float)

    # Alles, was die Quelle sonst noch mitgeschickt hat - Packspannung,
    # Strom, Kilometerstand, der unverrechnete Rohwert des Ladestands.
    # Gerechnet wird damit nicht; es liegt hier, damit sich später auswerten
    # lässt, was sich sonst nur durch eine zweite Fahrt klären liesse.
    rohwerte = Column(JSON)

    # Beim Eintreffen berechnet und mitgeschrieben, damit die Auswertung
    # später nicht die ganze Route erneut projizieren muss.
    km_auf_route = Column(Float)
    soll_soc = Column(Float)

    sitzung = relationship("LiveSitzung", back_populates="punkte")
