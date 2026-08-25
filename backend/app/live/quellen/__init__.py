"""Die Grenze zwischen jolt und dem, was im Auto misst.

Warum eine eigene Schicht, bevor überhaupt eine Quelle angeschlossen ist:
Ein ELM327-Dongle liest den Ladestand eines MEB-Fahrzeugs nicht über die
genormten OBD2-PIDs - die sind auf Verbrennungsmotoren gemünzt -, sondern
über herstellerspezifische UDS-Abfragen. Diese Kenntnis kauft man sich über
bestehende Software ein, statt sie nachzubauen. Damit steht aber fest, dass
die Messpunkte in *deren* Format ankommen und nicht in jolts. Welches Format
das sein wird, hängt an Telefon, App und Dongle und kann sich ändern; dass
übersetzt werden muss, steht fest.

Deshalb dieselbe Trennung wie bei `routing/provider.py`: Die Übersetzung ist
eine Datei je Format, und die Nachführung dahinter sieht nur `Rohpunkt`.

**Die Übersetzung kennt kein Netz.** Ein Normalisierer bekommt ein fertig
geparstes Objekt und gibt einen `Rohpunkt` zurück - mehr nicht. Ob dieses
Objekt aus einem POST an jolt kam, aus einer Antwort auf eine Abfrage bei
einem fremden Dienst oder aus einer Datei, ist eine Frage des Transports und
gehört nicht hierher. Das ist der Grund, warum `check_quellen.py` ohne Netz,
ohne Datenbank und ohne Zugangsdaten läuft - und warum sich ein neues Format
anhand einer aufgezeichneten Antwort einbauen lässt, ohne im Auto zu sitzen.

Fremde Daten sind bis zum Beweis des Gegenteils kaputt: fehlende Felder,
Prozent als Anteil statt als Prozentpunkte, Zeitstempel in Sekunden statt
Millisekunden, `null` mitten im Datensatz. Ein Normalisierer, der das nicht
abfängt, verlagert den Fehler nur - er landet dann als 500er im Log oder,
schlimmer, als stiller Unsinn im Energieprofil. Deshalb wirft jeder von ihnen
`QuellenFehler` mit einem Satz, der sagt, was fehlte.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class QuellenFehler(ValueError):
    """Die Meldung war nicht zu verwerten - mit einem Grund im Klartext."""


@dataclass
class Rohpunkt:
    """Ein Messpunkt, nachdem er aus einem fremden Format übersetzt wurde.

    Bewusst dieselben Felder wie `LivePunkt` - plus zwei, die manche Quellen
    mitliefern und jolt selbst nicht erheben kann:

    `zeit` ist der Zeitpunkt der **Messung**, nicht des Eintreffens. Ein
    Logger, der einen Funkloch-Puffer nachreicht, schickt fünf Punkte auf
    einmal; ohne diese Angabe lägen sie alle auf derselben Sekunde, und der
    Zeitfaktor wäre Unsinn.

    `laedt` ist die Aussage der Quelle, dass gerade geladen wird.
    `live/sitzung.py` erkennt Ladepausen heute am steigenden Ladestand, weil
    ihm nichts Besseres zur Verfügung steht - eine Quelle, die es direkt
    weiss, ist die bessere Auskunft. Das Feld wird hier mitgeführt, obwohl es
    noch niemand liest: Ein Übersetzer, der ein Feld des Formats wegwirft,
    ist ohne Not verlustbehaftet, und die Stelle, an der es gebraucht wird,
    steht schon fest.

    `soc` darf fehlen - dann ist es eine reine Positionsmeldung, und
    `live/sitzung.py` rechnet den Ladestand aus dem Energieprofil hoch. Für
    eine *fremde* Quelle ist er trotzdem Pflicht: Ein Logger im Auto, der den
    Ladestand nicht liefert, hat seinen einzigen Zweck verfehlt, und ihn
    stillschweigend als Positionsmelder durchzuwinken würde eine kaputte
    Einrichtung wie eine funktionierende aussehen lassen. Erzwungen wird das
    deshalb in den Übersetzern, nicht hier.
    """
    lat: float
    lon: float
    soc: float | None = None
    tempo_kmh: float | None = None
    aussentemp_c: float | None = None
    zeit: datetime | None = None
    laedt: bool | None = None


class Quelle(Protocol):
    #: Kurzname, unter dem das Format angesprochen wird ("jolt", "abrp", ...).
    name: str

    def normalisieren(self, daten: dict) -> Rohpunkt:
        """Eine Meldung dieses Formats in einen `Rohpunkt` übersetzen.

        Wirft `QuellenFehler`, wenn die Meldung nicht zu verwerten ist. Ein
        halb ausgefüllter Rohpunkt ist keine Option: Position und Ladestand
        sind das Minimum, mit dem sich ein Punkt auf die Route legen und
        gegen das Profil halten lässt.
        """
        ...


# ---------------------------------------------------------------------------
# Prüfungen, die jeder Übersetzer braucht
# ---------------------------------------------------------------------------

def zahl(daten: dict, *namen: str) -> float | None:
    """Den ersten vorhandenen Zahlenwert unter mehreren Feldnamen holen.

    Mehrere Namen, weil dasselbe Format je nach Version und Absender anders
    heisst - `ext_temp` und `extTemp` etwa. Ein `None` oder ein leerer String
    gilt als "nicht geliefert" und nicht als Null: Aussentemperatur 0 °C und
    "keine Aussentemperatur" sind zwei verschiedene Aussagen, und sie zu
    verwechseln heisst im Winter, die Heizung nicht zu rechnen.
    """
    for name in namen:
        if name not in daten:
            continue
        wert = daten[name]
        if wert is None or wert == "":
            continue
        try:
            gelesen = float(wert)
        except (TypeError, ValueError):
            raise QuellenFehler(f"Feld {name!r} ist keine Zahl: {wert!r}")
        # NaN kommt aus JSON zwar nicht, aus float("nan") aber sehr wohl - und
        # es vergiftet jede Rechnung dahinter lautlos, weil jeder Vergleich
        # damit False ergibt und keine Schranke greift.
        if gelesen != gelesen:
            raise QuellenFehler(f"Feld {name!r} ist keine Zahl: {wert!r}")
        return gelesen
    return None


def pflicht(daten: dict, *namen: str) -> float:
    wert = zahl(daten, *namen)
    if wert is None:
        raise QuellenFehler(f"Pflichtfeld fehlt: {' oder '.join(namen)}")
    return wert


def grenzen(wert: float, unten: float, oben: float, name: str) -> float:
    if not unten <= wert <= oben:
        raise QuellenFehler(
            f"{name} liegt ausserhalb des Möglichen: {wert:g} "
            f"(erwartet {unten:g} bis {oben:g})")
    return wert


def formate() -> dict:
    """Alle bekannten Formate, nach ihrem Kurznamen.

    Die Einfuhr steht in der Funktion und nicht am Kopf der Datei, weil die
    Übersetzer ihrerseits aus diesem Modul importieren - am Kopf wäre das ein
    Ringschluss. Ein Aufruf je Meldung ist das nicht wert: Python hält die
    Module nach der ersten Einfuhr im Speicher.
    """
    from .abrp import AbrpFormat
    from .jolt import JoltFormat
    return {q.name: q for q in (JoltFormat(), AbrpFormat())}


def finden(name: str) -> Quelle:
    """Den Übersetzer zu einem Formatnamen holen.

    Ein unbekannter Name ist ein Einrichtungsfehler und keine kaputte
    Meldung - deshalb nennt der Text die Formate, die es gibt, statt nur zu
    sagen, dass dieses es nicht ist.
    """
    bekannt = formate()
    quelle = bekannt.get((name or "").strip().lower())
    if quelle is None:
        raise QuellenFehler(
            f"Unbekanntes Meldeformat {name!r}. Bekannt sind: "
            f"{', '.join(sorted(bekannt))}.")
    return quelle


def wahrheit(daten: dict, *namen: str) -> bool | None:
    """Ein Ja/Nein-Feld lesen, das als bool, Zahl oder Text ankommen kann.

    Quellen sind sich hier bemerkenswert uneinig: `true`, `1`, `"1"`, `"true"`
    und `"yes"` sind alle schon vorgekommen. Was nicht zu deuten ist, gilt als
    "keine Aussage" - eine geratene Ladeerkennung wäre schlechter als keine.
    """
    for name in namen:
        if name not in daten or daten[name] is None:
            continue
        wert = daten[name]
        if isinstance(wert, bool):
            return wert
        if isinstance(wert, (int, float)):
            return bool(wert)
        text = str(wert).strip().lower()
        if text in ("1", "true", "yes", "ja"):
            return True
        if text in ("0", "false", "no", "nein"):
            return False
    return None
