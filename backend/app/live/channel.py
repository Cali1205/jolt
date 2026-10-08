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

_connections: dict[int, set] = {}
_lock = asyncio.Lock()


MAX_PER_SESSION = 10


async def sign_in(session_id: int, websocket) -> bool:
    """Nimmt die Verbindung auf; False, wenn die Sitzung schon voll ist."""
    async with _lock:
        open_ones = _connections.setdefault(session_id, set())
        if len(open_ones) >= MAX_PER_SESSION:
            return False
        open_ones.add(websocket)
        return True


async def sign_out(session_id: int, websocket) -> None:
    async with _lock:
        open_ones = _connections.get(session_id)
        if not open_ones:
            return
        open_ones.discard(websocket)
        if not open_ones:
            del _connections[session_id]


async def send(session_id: int, msg: dict) -> int:
    """Nachricht an alle Zuschauer einer Sitzung. Gibt die Anzahl zurück."""
    async with _lock:
        open_ones = list(_connections.get(session_id, ()))

    tot = []
    for connection in open_ones:
        try:
            await connection.send_json(msg)
        except Exception as failure:      # noqa: BLE001 - jeder Grund ist derselbe
            log.debug("Live-Verbindung entfernt (%s).", failure)
            tot.append(connection)

    for connection in tot:
        await sign_out(session_id, connection)
    return len(open_ones) - len(tot)


def viewer(session_id: int) -> int:
    return len(_connections.get(session_id, ()))
