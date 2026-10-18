"""Store login and logger tokens as SHA-256 hashes.

A database dump or backup should not hold usable credentials. Existing
tokens are converted in place (sha256 of the stored text), so logged-in
devices and configured loggers keep working: the clients still send the
original token, and the lookup now hashes it first.

Rows that already hold a 64-character value are left alone (a repeated run).
Not reversible: a hash cannot be turned back into the token.

Revision ID: 0018
Revises: 0017
"""
import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # In Python rather than SQL: the same code runs on Postgres and in the
    # SQLite of the checks.
    connection = op.get_bind()
    for table, column in (("auth_sessions", "token"), ("vehicles", "logger_token")):
        rows = connection.execute(sa.text(
            f"SELECT id, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND length({column}) <> 64")).fetchall()
        for row_id, value in rows:
            connection.execute(
                sa.text(f"UPDATE {table} SET {column} = :h WHERE id = :i"),
                {"h": hashlib.sha256(value.encode("utf-8")).hexdigest(), "i": row_id})


def downgrade() -> None:
    pass
