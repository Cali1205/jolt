"""Was eine Kilowattstunde an einem Ladepunkt kostet.

jolt kann das nicht wissen, und es steht auch in keiner der Importquellen:
Der Preis haengt am **Vertrag des Fahrers**, nicht an der Saeule. Dieselbe
Ionity-Saeule kostet mit Passport-Abo etwa die Haelfte von dem, was sie ad
hoc kostet, und wer eine EnBW-Karte hat, zahlt anderswo wieder anders.

Deshalb pflegt der Nutzer eine kurze Liste am Fahrzeug: ein Muster je
Anbieter und ein Standardpreis fuer alles Uebrige. Das ist wenig Aufwand -
man hat ein bis drei Karten - und es ist die einzige Angabe, die stimmen
kann.

Verglichen wird als Teilzeichenkette und klein geschrieben, wie bei den
bevorzugten Betreibern: "Ionity" trifft "Ionity GmbH", ohne dass der genaue
Wortlaut des Datensatzes bekannt sein muss.
"""

# Was eine Kilowattstunde kostet, wenn nichts anderes bekannt ist. Grob der
# Ad-hoc-Preis an einem Schnelllader in Deutschland - bewusst eher hoch:
# Ein zu niedriger Standardpreis liesse unbekannte Anbieter guenstiger
# aussehen als die, deren Preis man kennt, und genau die wuerden dann
# bevorzugt.
STANDARDPREIS_EUR_KWH = 0.59


def preis_je_kwh(betreiber: str, liste: list | None,
                 standard: float = STANDARDPREIS_EUR_KWH) -> float:
    """Der Preis fuer einen Betreiber, sonst der Standardpreis."""
    if not liste or not betreiber:
        return standard
    name = betreiber.strip().lower()
    for eintrag in liste:
        if not isinstance(eintrag, dict):
            continue
        muster = str(eintrag.get("muster") or "").strip().lower()
        if muster and muster in name:
            try:
                return float(eintrag.get("eur_kwh"))
            except (TypeError, ValueError):
                return standard
    return standard


def preisfunktion(fahrzeug):
    """Eine Funktion (Ladeoption) -> EUR/kWh fuer dieses Fahrzeug.

    Der Optimierer soll weder das ORM noch die Preisliste kennen - er
    bekommt eine Funktion, so wie er das Streckenprofil als Liste bekommt.
    Das haelt ihn ohne Datenbank pruefbar.
    """
    liste = getattr(fahrzeug, "strompreise", None) or []
    standard = getattr(fahrzeug, "strompreis_eur_kwh", None)
    standard = STANDARDPREIS_EUR_KWH if standard is None else float(standard)
    return lambda option: preis_je_kwh(getattr(option, "betreiber", ""),
                                       liste, standard)
