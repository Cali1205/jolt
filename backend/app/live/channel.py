"""WebSocket distributor for the live view.

Deliberately in process memory and without a broker: jolt runs as a single
container for one household. A Redis next to it would be a second thing that
can fail, for a problem that does not exist here.

Two properties are important nevertheless:

- Multiple connections per session. The phone rides along, the tablet on the
  passenger seat watches - both should see the same thing.
- A send error must not end the trip. A connection that dropped in a dead
  zone is removed silently; the samples keep flowing into the database.
"""
import asyncio
import logging

log = logging.getLogger("uvicorn.error")

_connections: dict[int, set] = {}
_lock = asyncio.Lock()


MAX_PER_SESSION = 10


async def sign_in(session_id: int, websocket) -> bool:
    """Accept the connection; False if the session is already full."""
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
    """Message to all viewers of a session. Returns the number reached."""
    async with _lock:
        open_ones = list(_connections.get(session_id, ()))

    tot = []
    for connection in open_ones:
        try:
            await connection.send_json(msg)
        except Exception as failure:      # noqa: BLE001 - every cause is the same
            log.debug("Live connection removed (%s).", failure)
            tot.append(connection)

    for connection in tot:
        await sign_out(session_id, connection)
    return len(open_ones) - len(tot)


def viewer(session_id: int) -> int:
    return len(_connections.get(session_id, ()))
