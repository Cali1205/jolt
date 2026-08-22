"""Alembic-Umgebung für jolt.

Zwei Dinge weichen bewusst vom Standard-Gerüst ab:

1. Die Datenbank-URL kommt aus `DATABASE_URL`, nicht aus alembic.ini - so
   steht kein Passwort in einer eingecheckten Datei.
2. `render_as_batch=True`, damit `alter_column` auch unter SQLite läuft.
   SQLite kann keine Spalten ändern; Alembic baut die Tabelle dann neu. Ohne
   das wären die Entwicklungs-DB und die Prüfläufe von den Migrationen
   ausgeschlossen - also genau von dem, was geprüft werden müsste.
"""
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base  # noqa: E402
from app import models  # noqa: E402,F401  - füllt Base.metadata

config = context.config
config.set_main_option(
    "sqlalchemy.url",
    os.environ.get("DATABASE_URL", "sqlite:///./jolt_dev.db").replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True, compare_type=False)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
