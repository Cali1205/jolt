"""WebSocket-Verteiler für die Live-Ansicht.

Bewusst im Prozessspeicher und ohne Broker: jolt läuft als ein Container für
einen Haushalt. Ein Redis daneben wäre ein zweites Ding, das ausfallen kann,
für ein Problem, das es hier nicht gibt.

Zwei Eigenschaften sind trotzdem wichtig:

- Mehrere Verbindungen je Sitzung. Das Telefon fährt mit, das Tablet am
  Beifahrersitz schaut zu - beide sollen dasselbe sehen.
- Ein Sendefehler darf die Fahrt nicht beenden. Eine Verbindung, die im
  Funkloch abgerissen ist, wird still entfernt; die Messpunkte laufen weiter
  in die Datenbank.
"""
import asyncio
import logging

log = logging.getLogger("uvicorn.error")

_verbindungen: dict[int, set] = {}
_sperre = asyncio.Lock()


MAX_JE_SITZUNG = 10


async def anmelden(sitzung_id: int, websocket) -> bool:
    """Nimmt die Verbindung auf; False, wenn die Sitzung schon voll ist."""
    async with _sperre:
        offene = _verbindungen.setdefault(sitzung_id, set())
        if len(offene) >= MAX_JE_SITZUNG:
            return False
        offene.add(websocket)
        return True


async def abmelden(sitzung_id: int, websocket) -> None:
    async with _sperre:
        offene = _verbindungen.get(sitzung_id)
        if not offene:
            return
        offene.discard(websocket)
        if not offene:
            del _verbindungen[sitzung_id]


async def senden(sitzung_id: int, nachricht: dict) -> int:
    """Nachricht an alle Zuschauer einer Sitzung. Gibt die Anzahl zurück."""
    async with _sperre:
        offene = list(_verbindungen.get(sitzung_id, ()))

    tot = []
    for verbindung in offene:
        try:
            await verbindung.send_json(nachricht)
        except Exception as fehler:      # noqa: BLE001 - jeder Grund ist derselbe
            log.debug("Live-Verbindung entfernt (%s).", fehler)
            tot.append(verbindung)

    for verbindung in tot:
        await abmelden(sitzung_id, verbindung)
    return len(offene) - len(tot)


def zuschauer(sitzung_id: int) -> int:
    return len(_verbindungen.get(sitzung_id, ()))
