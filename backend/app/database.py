import logging
import os

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker

# In the container DATABASE_URL comes from docker-compose (PostgreSQL). Locally
# without Postgres the app falls back to SQLite - that makes development and
# the check runs under tools/ possible without a running database.
#
# Precisely for this reason there is no PostGIS here either: the only geo
# query jolt needs ("all charging points in the corridor around a route")
# runs via an index on (lat, lon) plus haversine in Python. With 150,000
# charging points that costs milliseconds and keeps this fallback.
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
    """Bring the schema to the current state - exclusively via Alembic.

    No `create_all` alongside: two sources for the same schema inevitably
    drift apart, and the difference only shows up in production. The schema
    comes from the revisions, from nowhere else.
    """
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(here, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "..", "alembic"))
    command.upgrade(cfg, "head")


def seed_templates() -> None:
    """Provide vehicle templates if no vehicle has been created yet.

    Without a template one would have to enter the c_w value, frontal area and
    a charging curve by hand on first start - that is the point where one
    closes the app again. Templates are only created if the table is empty; a
    vehicle that has been adjusted once is never overwritten.
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
            "First vehicle created from template: %s", template["name"])
    finally:
        db.close()


def tables_present() -> bool:
    return bool(inspect(engine).get_table_names())
