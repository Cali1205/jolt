"""Tokens of cloud sources, kept in the database (`cloud_credentials`).

For sources whose refresh token rotates: every refresh returns a new one and
invalidates the old, so the new one has to be stored before anything else
uses the access token that came with it. `update` does that under a row lock,
which also keeps two refreshes from racing (the poller and the login tool,
or two app processes): the second waits and sees the first one's tokens.

Nothing here logs or returns the data to an API.
"""
from datetime import datetime
from typing import Callable

from .. import models


def _row(db, source: str, account: str, lock: bool = False):
    query = db.query(models.CloudCredential).filter_by(source=source, account=account)
    if lock:
        query = query.with_for_update()
    return query.one_or_none()


def load(db, source: str, account: str) -> dict | None:
    row = _row(db, source, account)
    return dict(row.data) if row else None


def save(db, source: str, account: str, data: dict) -> None:
    """Store the data, replacing what was there, and commit."""
    update(db, source, account, lambda _: data)


def update(db, source: str, account: str,
           change: Callable[[dict | None], dict | None]) -> dict | None:
    """Read under a row lock, let `change` decide, store and commit.

    `change` gets the stored data (None if there is none) and returns the
    new data, or None to leave it as it is. The result is what is stored
    afterwards. An exception in `change` rolls back and passes through.
    """
    try:
        row = _row(db, source, account, lock=True)
        current = dict(row.data) if row else None
        new = change(current)
        if new is None:
            db.rollback()
            return current
        if row is None:
            row = models.CloudCredential(source=source, account=account, data=new,
                                         updated_at=datetime.utcnow())
            db.add(row)
        else:
            row.data = dict(new)
            row.updated_at = datetime.utcnow()
        db.commit()
        return dict(new)
    except BaseException:
        db.rollback()
        raise
