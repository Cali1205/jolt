import logging
import os

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker

# Im Container kommt DATABASE_URL aus docker-compose (PostgreSQL). Lokal ohne
# Postgres fällt die App auf SQLite zurück - das macht Entwicklung und die
# Prüfläufe unter tools/ ohne laufende Datenbank möglich.
#
# Genau deshalb gibt es hier auch kein PostGIS: Der einzige Geo-Query, den jolt
# braucht ("alle Ladepunkte im Korridor um eine Route"), läuft über einen Index
# auf (lat, lon) plus Haversine in Python. Das kostet bei 150.000 Ladepunkten
# Millisekunden und erhält diesen Fallback.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./jolt_dev.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrate() -> None:
    """Schema auf den aktuellen Stand bringen - ausschliesslich über Alembic.

    Kein `create_all` daneben: Zwei Quellen für dasselbe Schema laufen
    unweigerlich auseinander, und der Unterschied fällt erst in der Produktion
    auf. Das Schema kommt aus den Revisionen, sonst nirgendwoher.
    """
    from alembic import command
    from alembic.config import Config

    hier = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(hier, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(hier, "..", "alembic"))
    command.upgrade(cfg, "head")


def seed_vorlagen() -> None:
    """Fahrzeug-Vorlagen bereitstellen, falls noch kein Fahrzeug angelegt ist.

    Ohne Vorlage müsste man beim ersten Start c_w-Wert, Stirnfläche und eine
    Ladekurve von Hand eintragen - das ist die Stelle, an der man die App
    wieder zumacht. Angelegt wird nur, wenn die Tabelle leer ist; ein einmal
    angepasstes Fahrzeug wird nie wieder überschrieben.
    """
    from . import models
    from .laden.kurven import VORLAGEN

    db = SessionLocal()
    try:
        if db.query(models.Fahrzeug).first():
            return
        vorlage = VORLAGEN[0]
        fahrzeug = models.Fahrzeug(**{k: v for k, v in vorlage.items()
                                      if k != "ladekurve"})
        db.add(fahrzeug)
        db.flush()
        for soc, kw in vorlage["ladekurve"]:
            db.add(models.Ladekurvenpunkt(fahrzeug_id=fahrzeug.id,
                                          soc_prozent=soc, kw=kw))
        db.commit()
        logging.getLogger("uvicorn.error").info(
            "Erstes Fahrzeug aus Vorlage angelegt: %s", vorlage["name"])
    finally:
        db.close()


def tabellen_vorhanden() -> bool:
    return bool(inspect(engine).get_table_names())
