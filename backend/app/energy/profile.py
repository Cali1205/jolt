"""Im gerechneten Energieprofil nachschlagen.

Das Profil ist eine Liste von Stützstellen entlang der Route, rund alle
250 m eine: `{"km", "soc", "kwh", "hoehe", "tempo_kmh", "minuten", "lat",
"lon"}`. Danach zu fragen ist eine der häufigsten Operationen in jolt - und
sie kam dreimal in zwei verschiedenen Bedeutungen vor, was der eigentliche
Grund für dieses Modul ist.

Die beiden Bedeutungen sind wirklich verschieden, und sie zu verwechseln
kostet Genauigkeit an genau den Stellen, an denen es zählt:

`wert_bei` **interpoliert** zwischen den Stützstellen. Das ist richtig für
alles, was verglichen oder verrechnet wird - der Soll-Ladestand an der
gemeldeten Position etwa. Bei 250 m Abstand ist die Gerade dazwischen genau
genug, und der Sprung von einer Stützstelle zur nächsten wäre bei einem
Ladestand mit zwei Nachkommastellen ein sichtbarer Fehler.

`eintrag_bei` gibt die **Stützstelle selbst** zurück, ungemischt. Das ist
richtig, wenn man eine zusammengehörige Zeile braucht - Position, Höhe und
Ladestand *desselben* Punktes. Zwischen zwei Stützstellen zu interpolieren
ergäbe hier eine Koordinate, die auf keiner Route liegt.
"""


def value_at(profile: list, km: float, field: str) -> float | None:
    """Einen einzelnen Wert an einem Kilometerstand, linear interpoliert."""
    if not profile:
        return None
    if km <= (profile[0].get("km") or 0.0):
        return profile[0].get(field)
    for earlier, after in zip(profile, profile[1:]):
        km1, km2 = earlier.get("km", 0.0), after.get("km", 0.0)
        if km1 <= km <= km2:
            if km2 <= km1:
                return earlier.get(field)
            share = (km - km1) / (km2 - km1)
            w1, w2 = earlier.get(field, 0.0), after.get(field, 0.0)
            return round(w1 + (w2 - w1) * share, 2)
    return profile[-1].get(field)


def soc_at(profile: list, km: float) -> float | None:
    """Der geplante Ladestand an einem Kilometerstand."""
    return value_at(profile, km, "soc")


def minutes_at(profile: list, km: float) -> float | None:
    """Die geplante **Fahrzeit** bis zu einem Kilometerstand.

    Ausdrücklich ohne Ladezeit - die steht im Ladeplan, nie im Profil. Wer
    das verwechselt, hält die Wanduhr gegen eine reine Fahrzeit und hat nach
    dem ersten Ladestopp eine Verspätung in Höhe der Ladedauer. Siehe
    `live/session.py`.
    """
    return value_at(profile, km, "mins")


def entry_at(profile: list, km: float) -> dict:
    """Die Stützstelle an einem Kilometerstand - die erste ab `km`.

    Leeres Profil ergibt ein leeres Objekt statt eines Fehlers: Aufrufer
    holen sich daraus einzelne Felder mit `.get`, und ein fehlendes Profil
    ist kein Grund abzustürzen.
    """
    if not profile:
        return {}
    if km <= (profile[0].get("km") or 0.0):
        return profile[0]
    for earlier, after in zip(profile, profile[1:]):
        if (earlier.get("km") or 0.0) <= km <= (after.get("km") or 0.0):
            return after
    return profile[-1]
