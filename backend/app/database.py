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

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(here, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "..", "alembic"))
    command.upgrade(cfg, "head")


def seed_templates() -> None:
    """Fahrzeug-Vorlagen bereitstellen, falls noch kein Fahrzeug angelegt ist.

    Ohne Vorlage müsste man beim ersten Start c_w-Wert, Stirnfläche und eine
    Ladekurve von Hand eintragen - das ist die Stelle, an der man die App
    wieder zumacht. Angelegt wird nur, wenn die Tabelle leer ist; ein einmal
    angepasstes Fahrzeug wird nie wieder überschrieben.
    """
    from . import models
    from .charging.curves import TEMPLATES

    db = SessionLocal()
    try:
        if db.query(models.Vehicle).first():
            return
        template = TEMPLATES[0]
        vehicle = models.Vehicle(**{k: v for k, v in template.items()
                                      if k != "charge_curve"})
        db.add(vehicle)
        db.flush()
        for soc, kw in template["charge_curve"]:
            db.add(models.ChargeCurvePoint(vehicle_id=vehicle.id,
                                          soc_percent=soc, kw=kw))
        db.commit()
        logging.getLogger("uvicorn.error").info(
            "Erstes Fahrzeug aus Vorlage angelegt: %s", template["name"])
    finally:
        db.close()


def tables_present() -> bool:
    return bool(inspect(engine).get_table_names())
