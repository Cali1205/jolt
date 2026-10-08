"""Eine Messreihe in Fahr- und Ladeabschnitte zerlegen.

**Warum es dieses Modul gibt.** Die Frage "hat das Auto zwischen diesen
beiden Messpunkten geladen?" wurde an fünf Stellen unabhängig beantwortet -
mit drei Konstanten in drei Modulen, zwei verschiedenen Schwellen (0,5 und
1,0 Prozentpunkte) und zwei verschiedenen Formen (paarweise gegen den
Vorgänger, oder gegen das Minimum eines Zeitfensters). Keine der fünf wusste
von den anderen.

Das war keine Geschmacksfrage, sondern die Quelle mehrerer Fehler. Ein
Ladestopp ist für fast jede Auswertung einer Messreihe ein Sonderfall, und
wer ihn nicht kennt, rechnet still falsch:

* Die Kalibrierung nahm Anfang minus Ende. Nachgeladene Prozentpunkte fehlten
  in dieser Differenz - aus einer Fahrt mit 29 kWh/100 km wurde ein gelernter
  Wert von 12, und weil er in den Plausibilitätsgrenzen blieb, fiel es nicht
  auf.
* Die Abweichung verglich den Ladestand gegen ein Profil, das keine
  Ladestopps kennt, und meldete danach dreistellige Prozentpunkte.

Behoben wurde beides, indem die Erkennung dort **noch einmal** geschrieben
wurde. Beim nächsten Modul, das Messpunkte auswertet, wäre dasselbe passiert.
Deshalb steht sie jetzt einmal hier.

**Warum in `energie` und nicht in `live`.** `energie.kalibrierung` gehört zu
den Nutzern, und `energie` liegt unter `live` - andersherum entstünde ein
Zyklus. Inhaltlich passt es: Es geht um die Deutung einer Messreihe, so wie
bei der Kalibrierung daneben.

**Was ein Messpunkt sein muss.** Nichts weiter als ein Objekt mit `soc`,
`zeit` und `km_auf_route`. Bewusst kein ORM-Typ: `energie` kennt die
Datenbank nicht, und das soll so bleiben - nur deshalb lassen sich die
Prüfskripte ohne Anwendung laufen.
"""
from dataclasses import dataclass
from datetime import timedelta

# Ab wie viel Anstieg ein Abschnitt als Ladevorgang gilt. Darunter ist es
# Rekuperation oder das Rauschen der SoC-Messung (meist 0,5 % Auflösung), und
# beides gehört zur Fahrt. **Die eine Schwelle**, vorher drei.
CHARGING_PP = 0.5


@dataclass(frozen=True)
class Section:
    """Das Stück zwischen zwei aufeinanderfolgenden Messpunkten.

    `soc_pp` ist positiv, wenn verbraucht wurde, und negativ beim Laden -
    also in Richtung "was hat es gekostet", nicht in Richtung "wie hat sich
    der Ladestand verändert". Das ist die Blickrichtung aller Nutzer.
    """
    begin: object
    past: object
    charges: bool
    soc_pp: float
    km: float

    @property
    def charged_pp(self) -> float:
        """Wie viel in diesem Abschnitt nachgeladen wurde, sonst null."""
        return -self.soc_pp if self.charges else 0.0

    @property
    def mins(self) -> float | None:
        if not getattr(self.begin, "timestamp", None) or not getattr(self.past, "timestamp", None):
            return None
        return (self.past.timestamp - self.begin.timestamp).total_seconds() / 60.0


def sections(points) -> list[Section]:
    """Die Messreihe paarweise zerlegen, jeder Abschnitt mit Ladekennzeichen.

    Nur Punkte mit **gemeldetem** Ladestand zählen. An einem Punkt ohne
    Ladestand lässt sich kein Anstieg ablesen, und ein hochgerechneter Wert
    wäre hier besonders schädlich: Er stammt aus demselben Modell, das die
    Nutzer dieser Funktion gerade prüfen wollen.

    Die Zeit zwischen zwei Meldungen geht dadurch nicht verloren - sie steckt
    dann in einem grösseren Sprung, und wer sie braucht, findet sie über
    `Abschnitt.minuten`.
    """
    with_soc = [p for p in points if p.soc is not None]
    result = []
    for earlier, after in zip(with_soc, with_soc[1:]):
        soc_pp = earlier.soc - after.soc
        result.append(Section(
            begin=earlier, past=after,
            charges=soc_pp <= -CHARGING_PP,
            soc_pp=soc_pp,
            km=((after.km_on_route or 0.0) - (earlier.km_on_route or 0.0))))
    return result


def charged_pp(points, until_point=None) -> float:
    """Wie viele Prozentpunkte insgesamt nachgeladen wurden.

    `bis_punkt` begrenzt auf den Anfang der Reihe bis zu diesem Punkt -
    gebraucht für die Abweichung während der Fahrt, die wissen muss, was
    **bisher** geladen wurde.
    """
    total = 0.0
    for section in sections(points):
        total += section.charged_pp
        if until_point is not None and section.past is until_point:
            break
    return total


def consumption(points) -> tuple[float, float]:
    """(verbrauchte Prozentpunkte, dabei gefahrene Kilometer).

    Ladeabschnitte bleiben aussen vor - sie sagen nichts über den Verbrauch,
    und ihre Strecke darf auch nicht mitzählen, sonst stünde sie im Nenner
    ohne den zugehörigen Verbrauch im Zähler.
    """
    pp = km = 0.0
    for section in sections(points):
        if section.charges:
            continue
        pp += section.soc_pp
        km += section.km
    return pp, km


def charge_pauses_minutes(points, driven_minutes) -> float:
    """Wie viel der verstrichenen Zeit auf Ladepausen entfiel.

    `gefahrene_minuten(von_km, bis_km)` liefert die **Fahrzeit** für ein
    Stück Strecke; abgezogen wird sie, weil eine Talfahrt den Ladestand auch
    hebt, aber keine zusätzliche Zeit kostet. Übrig bleibt die Standzeit.

    Gebraucht wird das, weil das Energieprofil ausschliesslich Fahrzeit führt:
    Die Ladezeit steht im Plan, nie im Profil. Wer die Wanduhr ungefiltert
    dagegen hält, sieht nach dem ersten Ladestopp eine Verspätung in Höhe der
    Ladedauer - dauerhaft, denn sie wird nie wieder aufgeholt.
    """
    total = 0.0
    for section in sections(points):
        if not section.charges:
            continue
        elapsed = section.mins
        if elapsed is None:
            continue
        driven = driven_minutes(section.begin.km_on_route or 0.0,
                                     section.past.km_on_route or 0.0)
        total += max(0.0, elapsed - (driven or 0.0))
    return total


def charges_at_end(points, timeframe_minutes: float,
                  min_swing_pp: float) -> bool:
    """Sah der Schluss der Messreihe nach einem Ladevorgang aus?

    Eine andere Frage als `abschnitte`, deshalb eine eigene Schwelle: Hier
    geht es nicht darum, ob ein einzelner Abschnitt lädt, sondern ob am Ende
    **genug** nachgeladen wurde, um von einer Ladepause auszugehen. Der
    Aufrufer entscheidet, wie viel genug ist - für das Aufräumen einer
    vergessenen Fahrt darf die Schwelle höher liegen als für eine Bilanz.
    """
    with_soc = [p for p in points if p.soc is not None and p.timestamp is not None]
    if len(with_soc) < 2:
        return False
    end = with_soc[-1].timestamp
    timeframe = [p for p in with_soc
               if end - p.timestamp <= timedelta(minutes=timeframe_minutes)]
    return charged_pp(timeframe) >= min_swing_pp
