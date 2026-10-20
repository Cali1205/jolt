#!/usr/bin/env python3
"""Checks the signal catalog (backend/app/signals.py) against everything
that speaks it: the docs, the sources, stored data and the signals API.

Without network, without Postgres:

    ./tools/check_signals.py            check
    ./tools/check_signals.py --write    regenerate the table in docs/data-model.md
"""
import os
import re
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from examine import Check, application_provide  # noqa: E402

application_provide("signals")

from app import signals  # noqa: E402

verify = Check()
ROOT = os.path.join(TOOLS, "..")
DOC = os.path.join(ROOT, "docs", "data-model.md")
TABLE = re.compile(r"(<!-- signals:start -->\r?\n)(.*?)(\r?\n<!-- signals:end -->)", re.S)


def _doc() -> str:
    with open(DOC, encoding="utf-8", newline="") as file:
        return file.read()


def write_doc() -> None:
    text = _doc()
    newline = "\r\n" if "\r\n" in text else "\n"
    table = signals.markdown_table().replace("\n", newline)
    with open(DOC, "w", encoding="utf-8", newline="") as file:
        file.write(TABLE.sub(lambda m: m.group(1) + table + m.group(3), text))


def part_catalog():
    verify.section("Katalog")
    names = [s.name for s in signals.SIGNALS]
    verify(len(names) == len(set(names)), "jeder Signalname kommt einmal vor")
    verify(all(re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names),
           "Namen sind klein, mit Unterstrich")
    verify([s.name for s in signals.SIGNALS if s.tier == "required"] == ["soc_pct"],
           "Pflicht ist allein der Ladestand")
    verify(all(s.tier in signals.TIERS and s.kind in ("number", "bool", "enum", "text")
               for s in signals.SIGNALS), "Stufe und Art aus der festen Liste")
    verify(all(bool(s.values) == (s.kind == "enum") for s in signals.SIGNALS),
           "genau die Aufzählungen nennen ihre Werte")
    verify(all(s.label and s.meaning for s in signals.SIGNALS),
           "jedes Signal hat Bezeichnung und Bedeutung")
    for name in ("odometer_km", "speed_kmh", "outside_temp_c", "batt_temp_c",
                 "charge_power_kw", "charge_state", "plugged"):
        verify(signals.BY_NAME[name].tier == "recommended", f"{name} ist empfohlen")
    verify(signals.CHARGE_STATES == ("charging", "ready", "not_ready", "purpose_reached",
                                     "conservation", "error"),
           "Ladezustände wie in der Erkennung", str(signals.CHARGE_STATES))


def part_doc():
    verify.section("docs/data-model.md")
    match = TABLE.search(_doc())
    table = match.group(2).replace("\r\n", "\n") if match else ""
    verify(table == signals.markdown_table(),
           "die Tabelle entspricht signals.py (sonst: check_signals.py --write)")


PARTS = [part_catalog, part_doc]


def main() -> int:
    if "--write" in sys.argv:
        write_doc()
        print("docs/data-model.md neu geschrieben.")
        return 0
    for part in PARTS:
        part()
    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
