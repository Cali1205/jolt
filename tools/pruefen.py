"""Das Gerüst, das alle Prüfskripte teilen.

Sechs Skripte hatten dieselbe `pruefe`-Funktion - in fünf davon byteweise
identisch -, dieselbe Fehlerliste, dieselbe Auswertung am Ende und dieselbe
Präambel, die den Suchpfad setzt und eine Wegwerf-Datenbank einrichtet. Das
ist nicht viel Code, aber es ist Code, der sechsmal auseinanderlaufen kann,
und zweimal war er es schon.

Bewusst kein Testrahmen von der Stange. Was die Skripte tun, ist keine
Sammlung von Zusicherungen, sondern eine **lesbare Behauptung je Zeile**:
"kalte Luft ist mindestens 7 % dichter als warme". Diese Sätze sind der Sinn
der Sache - sie stehen so in der Ausgabe, und wer sie liest, weiss, was jolt
über sich selbst behauptet. Ein Rahmen, der stattdessen
`test_luftdichte_kalt PASSED` ausgibt, hätte diesen Nutzen nicht.

Ebenso bewusst ohne Abhängigkeiten: Die Skripte laufen ohne Netz, ohne
Postgres und ohne API-Schlüssel, und `check_modell`, `check_optimierer` und
`check_quellen` laufen sogar ohne installierte Anwendung. Das bleibt so.
"""
import os
import sys
import tempfile


class Pruefung:
    """Sammelt Ergebnisse und weiss am Ende, ob etwas fehlt.

    Eine Klasse und keine Modulvariablen, damit zwei Skripte im selben
    Prozess sich nicht die Fehlerliste teilen - beim Zusammenfassen mehrerer
    Prüfungen ist das sonst eine stille Fehlerquelle.
    """

    def __init__(self) -> None:
        self.fehler: list[str] = []

    def __call__(self, bedingung, text: str, zusatz: str = "") -> None:
        """Eine Behauptung prüfen und sie in einem Satz protokollieren.

        `zusatz` erscheint nur im Fehlerfall und soll den **gemessenen Wert**
        tragen, nicht die Wiederholung der Behauptung: Wer sieht, dass 2,077
        herauskam statt 1,23, weiss sofort, wonach er sucht.
        """
        if bedingung:
            print(f"  ok    {text}")
        else:
            print(f"  FEHLT {text}   {zusatz}")
            self.fehler.append(text)

    def abschnitt(self, titel: str) -> None:
        print(f"\n{titel}")

    def bilanz(self, nachsatz: str = "") -> int:
        """Rückgabewert für `sys.exit` - 0, wenn alles hielt.

        `nachsatz` ist für das, was ein Skript *nicht* prüfen kann. Diese
        Einschränkung gehört in die Ausgabe und nicht nur in den Quelltext:
        Ein bestandener Lauf, der verschweigt, was er nicht angefasst hat,
        weckt mehr Vertrauen als er verdient.
        """
        print()
        if self.fehler:
            print(f"{len(self.fehler)} Prüfung(en) fehlgeschlagen:")
            for text in self.fehler:
                print(f"  - {text}")
            return 1
        print("Alle Prüfungen bestanden.")
        if nachsatz:
            print(f"\n{nachsatz}")
        return 0


def anwendung_bereitstellen(marke: str, *, datenbank: bool = True) -> None:
    """Suchpfad setzen und, falls nötig, eine Wegwerf-Datenbank einrichten.

    **Vor jedem App-Import aufrufen.** `app.database` baut die Engine schon
    beim Import; wer `DATABASE_URL` danach setzt, ändert nichts mehr und
    schreibt in die Entwicklungsdatenbank - im schlimmsten Fall in die echte.

    `ORS_API_KEY` und `APP_PASSWORT` werden entfernt, damit ein Lauf auf
    einem eingerichteten Rechner dasselbe tut wie auf einem nackten: Demo-
    Routing, kein Login. Ein Prüfskript, dessen Ergebnis von der Umgebung
    abhängt, prüft die Umgebung.
    """
    hier = os.path.dirname(os.path.abspath(__file__))
    pfad = os.path.join(hier, "..", "backend")
    if pfad not in sys.path:
        sys.path.insert(0, pfad)

    if datenbank:
        ordner = tempfile.mkdtemp(prefix=f"jolt-{marke}-")
        os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(ordner, 'check.db')}"
    os.environ.pop("ORS_API_KEY", None)
    os.environ.pop("APP_PASSWORT", None)
