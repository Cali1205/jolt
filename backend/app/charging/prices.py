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
DEFAULT_PRICE_EUR_KWH = 0.59


def price_per_kwh(operator: str, lst: list | None,
                 std_default: float = DEFAULT_PRICE_EUR_KWH) -> float:
    """Der Preis fuer einen Betreiber, sonst der Standardpreis."""
    if not lst or not operator:
        return std_default
    name = operator.strip().lower()
    for entry in lst:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("pattern") or "").strip().lower()
        if pattern and pattern in name:
            try:
                return float(entry.get("eur_kwh"))
            except (TypeError, ValueError):
                return std_default
    return std_default


def price_function(vehicle):
    """Eine Funktion (Ladeoption) -> EUR/kWh fuer dieses Fahrzeug.

    Der Optimierer soll weder das ORM noch die Preisliste kennen - er
    bekommt eine Funktion, so wie er das Streckenprofil als Liste bekommt.
    Das haelt ihn ohne Datenbank pruefbar.
    """
    lst = getattr(vehicle, "electricity_prices", None) or []
    std_default = getattr(vehicle, "electricity_price_eur_kwh", None)
    std_default = DEFAULT_PRICE_EUR_KWH if std_default is None else float(std_default)
    return lambda option: price_per_kwh(getattr(option, "operator", ""),
                                       lst, std_default)
