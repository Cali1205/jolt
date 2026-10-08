"""What a kilowatt hour costs at a charge point.

jolt cannot know this, and none of the import sources contain it either:
the price depends on the **driver's contract**, not on the charger. The same
Ionity charger costs about half as much with a Passport subscription as it
does ad hoc, and anyone with an EnBW card pays something different again
elsewhere.

So the user maintains a short list on the vehicle: one pattern per provider
and a default price for everything else. That is little effort - you have
one to three cards - and it is the only information that can be correct.

Matching is a lowercase substring comparison, as with the preferred
operators: "Ionity" matches "Ionity GmbH" without needing to know the exact
wording of the record.
"""

# What a kilowatt hour costs when nothing else is known. Roughly the ad-hoc
# price at a fast charger in Germany - deliberately on the high side: a
# default price that is too low would make unknown providers look cheaper
# than those whose price is known, and those are exactly the ones that would
# then be preferred.
DEFAULT_PRICE_EUR_KWH = 0.59


def price_per_kwh(operator: str, lst: list | None,
                 std_default: float = DEFAULT_PRICE_EUR_KWH) -> float:
    """The price for an operator, otherwise the default price."""
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
    """A function (charge option) -> EUR/kWh for this vehicle.

    The optimizer should know neither the ORM nor the price list - it gets
    a function, just as it gets the route profile as a list. That keeps it
    testable without a database.
    """
    lst = getattr(vehicle, "electricity_prices", None) or []
    std_default = getattr(vehicle, "electricity_price_eur_kwh", None)
    std_default = DEFAULT_PRICE_EUR_KWH if std_default is None else float(std_default)
    return lambda option: price_per_kwh(getattr(option, "operator", ""),
                                       lst, std_default)
