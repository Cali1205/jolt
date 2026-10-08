"""Data model of jolt.

The division follows the question of what changes how often: vehicle
parameters rarely, charging curves occasionally, charging points on every
import, live measurement points every second.
"""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import relationship

from .database import Base


class AuthSession(Base):
    """A logged-in device. One token per device, so that one can be logged out
    individually without taking the others along."""
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    device = Column(String(120), default="")
    created = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)


class PushSubscription(Base):
    """A device that wants to receive notifications.

    The `endpoint` is the address at the push service assigned by the browser
    and at the same time the key: the same browser delivers it again as long
    as the permission exists. That is why it is unique - a second subscription
    of the same device would mean that the same phone gets every message twice.

    The two keys belong to the device, not to the server: with them the payload
    is encrypted so that the push service passes it on without being able to
    read it.
    """
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True)
    endpoint = Column(String(500), unique=True, nullable=False, index=True)
    p256dh = Column(String(200), nullable=False)
    auth = Column(String(100), nullable=False)
    device = Column(String(120), default="")

    # Consecutive failed attempts. A dead subscription is deleted immediately
    # (404/410); this counter only shows that a device is permanently
    # unreachable without having unsubscribed.
    failure = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Vehicle(Base):
    """Everything the consumption model needs to know about the car.

    The parameters are deliberately physical and not "kWh/100 km": only that
    way can it be answered what 130 instead of 110 km/h costs or what a pass
    consumes. The flat value cannot do that - see konzept-routenplaner.md.
    """
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)

    battery_gross_kwh = Column(Float, nullable=False)
    # Usable is always less than gross - the buffer at the top and bottom
    # belongs to the battery management. Only net is used for calculations.
    battery_net_kwh = Column(Float, nullable=False)

    curb_mass_kg = Column(Float, nullable=False, default=1800.0)
    payload_kg = Column(Float, nullable=False, default=150.0)

    c_w = Column(Float, nullable=False, default=0.28)
    frontal_area_m2 = Column(Float, nullable=False, default=2.3)
    c_rr = Column(Float, nullable=False, default=0.010)

    # Top speed of the vehicle in km/h. NULL = no limit in the model. The
    # planning speed slider runs into it, instead of letting the model
    # calculate with speeds that the car does not drive.
    max_speed_kmh = Column(Float)

    eta_drive = Column(Float, nullable=False, default=0.88)
    # Regeneration never recovers everything. That is why the balance over a
    # pass is negative, even though one ends up at the starting elevation.
    eta_regen = Column(Float, nullable=False, default=0.70)

    # Base load independent of the heating: control units, lights, pumps.
    p_aux_w = Column(Float, nullable=False, default=350.0)
    # The largest single difference in winter: a heat pump needs roughly half
    # of what an electric heater needs for the same cabin temperature. At
    # -5 °C that is about 1.8 kW difference - over four hours of driving more
    # than 7 kWh, i.e. the reason for an additional charging stop.
    heat_pump = Column(Boolean, nullable=False, default=True)

    reserve_soc = Column(Float, nullable=False, default=10.0)
    target_soc = Column(Float, nullable=False, default=20.0)

    max_charge_power_kw = Column(Float, nullable=False, default=150.0)
    connector_type = Column(String(20), nullable=False, default="CCS")

    # Names (or parts of them, e.g. "EnBW") that the optimiser prefers when
    # choosing stops - see charging/availability.py:operator_bonus().
    # Not a hard filter: a non-preferred provider remains selectable, it just
    # does not get an additional advantage.
    preferred_operators = Column(JSON, nullable=False, default=list)

    # What a kilowatt hour costs - on the vehicle, because it depends on the
    # contract and not on the charger. `electricity_prices` is a list of
    # {pattern, eur_kwh}, `electricity_price_eur_kwh` applies to everything
    # else. See charging/prices.py.
    electricity_price_eur_kwh = Column(Float, nullable=False, default=0.59)
    electricity_prices = Column(JSON)

    # Learned from real trips (energy/calibration.py). 1.0 = unchecked.
    correction_factor = Column(Float, nullable=False, default=1.0)
    # What the vehicle itself says about its capacity (DID 222AB2),
    # and when. NULL means "never measured" - see migration 0014.
    measured_capacity_kwh = Column(Float)
    capacity_measured_at = Column(DateTime)

    # Long-lived secret for a logger in the car - OBD2 dongle, shortcut,
    # whatever. It cannot know the ID of the running live session: that is only
    # created when setting off in the app and changes with every trip. A device
    # that is installed in the car and simply starts sending when switched on
    # therefore needs a key that stays - the backend then finds the running
    # session of this vehicle by itself.
    # NULL means "no logger set up".
    logger_token = Column(String(64), unique=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def capacity_kwh(self) -> float:
        """The capacity that is used for calculations - measured before brochure.

        The value in the profile is the manufacturer's figure for a new
        vehicle. If the car itself reports a number, it is better: it describes
        **this** battery in **this** condition. For the ID.Buzz after almost
        60,000 km that is 73.8 instead of 77 kWh - four percent that would
        otherwise be wrong consistently in the same direction.

        The bound catches a nonsensical measurement: a value above the
        brochure value or below half of it is not ageing but a reading error,
        and then the profile applies.
        """
        measured = self.measured_capacity_kwh
        if measured and 0.5 * self.battery_net_kwh <= measured <= self.battery_net_kwh * 1.05:
            return measured
        return self.battery_net_kwh

    charge_curve = relationship("ChargeCurvePoint", back_populates="vehicle",
                             cascade="all, delete-orphan",
                             order_by="ChargeCurvePoint.soc_percent")

    @property
    def mass_kg(self) -> float:
        return self.curb_mass_kg + self.payload_kg


class ChargeCurvePoint(Base):
    """Support point of the charging curve: at this SoC this power.

    A table of its own instead of a JSON field on the vehicle, because the
    curve is what one refines after the first real charging sessions -
    independently of all other vehicle data.
    """
    __tablename__ = "charge_curve_points"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    soc_percent = Column(Float, nullable=False)
    kw = Column(Float, nullable=False)

    vehicle = relationship("Vehicle", back_populates="charge_curve")

    __table_args__ = (UniqueConstraint("vehicle_id", "soc_percent",
                                       name="uq_ladekurve_soc"),)


class ChargePoint(Base):
    """A charging location from one of the import sources.

    `connectors` is deliberately JSON: the sources deliver varying numbers of
    plugs with varying power, and turning each into an object of its own
    would gain nothing - filtering is done via `max_kw` and `connector_types`,
    both written along during import.
    """
    __tablename__ = "charge_points"

    id = Column(Integer, primary_key=True)
    source = Column(String(20), nullable=False)      # "bnetza" | "ocm"
    foreign_id = Column(String(80), nullable=False)

    name = Column(String(200), default="")
    operator = Column(String(200), default="")
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    address = Column(String(250), default="")
    # For locations that cover several postcodes (large shopping centres, for
    # example), OCM delivers a semicolon list instead of a single postcode -
    # "33000;33100;33200;33300;33800" is 29 characters long and aborted the
    # OCM import on a real data set ("Auchan Bordeaux Lac").
    postcode = Column(String(40), default="")
    city = Column(String(120), default="")
    country = Column(String(2), default="DE")

    connectors = Column(JSON, default=list)
    # Denormalised, so that the corridor query can filter without evaluating
    # JSON - JSON access differs between SQLite and Postgres.
    max_kw = Column(Float, nullable=False, default=0.0)
    point_count = Column(Integer, nullable=False, default=1)
    connector_types = Column(String(120), default="")   # "CCS,Typ2"

    as_of = Column(String(20), default="")

    # What makes a charging point unusable for a specific trip is stated in
    # words in the sources - and was thrown away until now. See migration
    # 0013.
    #
    # NULL means **unknown** for the boolean values and not "no": for the
    # largest part of the database the information does not exist, and
    # whoever treats unknown like excluded loses almost everything.
    operational = Column(Boolean)
    access = Column(String(60))
    membership_required = Column(Boolean)
    # Free text from the source: costs, access notes, comments. For now only
    # kept - see migration 0013.
    hints = Column(JSON)

    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "foreign_id", name="uq_ladepunkt_quelle"),
        # The corridor always queries via a rectangle: first lat, then lon.
        Index("ix_ladepunkt_pos", "lat", "lon"),
        Index("ix_ladepunkt_kw", "max_kw"),
    )


class Trip(Base):
    """A planned trip including the calculated energy profile."""
    __tablename__ = "trips"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False)

    start_text = Column(String(250), default="")
    start_lat = Column(Float, nullable=False)
    start_lon = Column(Float, nullable=False)
    target_text = Column(String(250), default="")
    target_lat = Column(Float, nullable=False)
    target_lon = Column(Float, nullable=False)

    start_soc = Column(Float, nullable=False)
    speed_factor = Column(Float, nullable=False, default=1.0)
    outside_temp_c = Column(Float)
    # Payload of this trip. NULL means "the vehicle profile applied" - that way
    # trips from before this field remain correctly readable, instead of
    # retroactively claiming a payload of 0 kg.
    payload_kg = Column(Float)

    # Surcharge on air resistance for everything hanging on the outside -
    # bike carrier, roof box. 1.0 means "nothing attached". Belongs to the trip
    # and not to the vehicle: the same route once with and once without a
    # carrier are two different energy balances, and the carrier is on in
    # summer and not in winter.
    air_drag_factor = Column(Float, nullable=False, default=1.0)

    # A trailer belongs to the trip: mass in kg and additional air-resistance
    # area (c_w times A) in m². NULL means "none". Deliberately not absorbed
    # into `air_drag_factor`: a caravan does not double the car's c_w value, it
    # brings an area of its own and 1.3 t on top.
    trailer_kg = Column(Float)
    trailer_cwa_m2 = Column(Float)
    # Top speed of this trip in km/h - 100 for a combination with trailer, as a
    # hard limit and not as a preference. NULL = none beyond that of the
    # vehicle.
    speed_max_kmh = Column(Float)

    # Recorded instead of planned: geometry and energy profile are then empty
    # at the start and are created from the measurement points when it ends.
    # See live/recording.py.
    recording = Column(Boolean, nullable=False, default=False)

    distance_m = Column(Float, default=0.0)
    drive_time_s = Column(Float, default=0.0)

    # Display geometry, thinned out to ~1 point per 250 m. The full ORS
    # response has a five-digit number of support points on a long route;
    # neither the map nor the forecast needs them.
    geometry = Column(JSON, default=list)          # [[lon, lat, elevation], ...]
    energy_profile = Column(JSON, default=list)      # [{"km","soc","kwh"}, ...]

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    vehicle = relationship("Vehicle")
    # So that the history can distinguish "planned" from "actually driven",
    # and deleting a trip takes its sessions along instead of failing on the
    # foreign key constraint.
    live_sessions = relationship("LiveSession", back_populates="trip",
                                  cascade="all, delete-orphan")


class LiveSession(Base):
    """A running trip into which measurement points arrive.

    The source of the measurement points is deliberately open: today the PWA
    or the simulator, later the OBD2 logger or a manufacturer API. That does
    not change the schema - only who POSTs.
    """
    __tablename__ = "live_sessions"

    id = Column(Integer, primary_key=True)
    trip_id = Column(Integer, ForeignKey("trips.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at = Column(DateTime)
    running = Column(Boolean, default=True, nullable=False)

    # Running consumption factor: actual divided by planned over the last
    # kilometres. > 1 means "consumes more than calculated".
    consumption_factor = Column(Float, default=1.0, nullable=False)
    # The same for time. The consumption factor alone does not see a traffic
    # jam: someone stuck in a jam even consumes more per kilometre, but the
    # arrival time shifts by a multiple of that. For the trigger "arrival time
    # shifts" a number of its own is therefore needed.
    time_factor = Column(Float, default=1.0, nullable=False)
    hint = Column(Text, default="")

    # The currently valid charging plan. Calculated at the start of the trip
    # and replaced on the road as soon as a trigger fires. It lives here and
    # not on the trip, because it belongs to the *running* trip: driving the
    # same planned route a second time results in a different plan.
    plan = Column(JSON)
    # Since when the vehicle has been off the route. The threshold is "more
    # than 500 m for more than a minute" - without this timestamp every
    # inaccurate GPS reading at a bridge would be a re-plan.
    detour_since = Column(DateTime)

    trip = relationship("Trip", back_populates="live_sessions")
    points = relationship("LivePoint", back_populates="session",
                          cascade="all, delete-orphan",
                          order_by="LivePoint.timestamp")


class LivePoint(Base):
    __tablename__ = "live_points"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("live_sessions.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    # NULL means "position reported, charge level not known". The two
    # quantities have different rates: the phone delivers the position every
    # second and for free, the charge level is typed in by someone when
    # standing at the charger anyway. What applies in between is extrapolated
    # by `live/session.py` from the energy profile.
    soc = Column(Float)
    # Raw measurement, deliberately not the basis of the speed factor. That is
    # formed from distance and time over a window of kilometres
    # (`live/session.py`), for two reasons: the instantaneous speed from GPS is
    # noisy, and on iOS `coords.speed` regularly delivers nothing at all. A
    # factor based on a field that is missing depending on the phone would not
    # be a factor.
    #
    # It is kept anyway: it costs four bytes per measurement point and is the
    # only thing by which one could later verify whether the calculation from
    # distance and time agrees with what the speedometer saw.
    speed_kmh = Column(Float)
    outside_temp_c = Column(Float)

    # Everything else the source sent along - pack voltage, current, odometer
    # reading, the unprocessed raw value of the charge level. Not used for
    # calculations; it is kept here so that later one can evaluate what could
    # otherwise only be clarified by a second trip.
    raw_values = Column(JSON)

    # Calculated and written along on arrival, so that the evaluation later
    # does not have to project the whole route again.
    km_on_route = Column(Float)
    plan_soc = Column(Float)

    session = relationship("LiveSession", back_populates="points")
