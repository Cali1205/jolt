"""Client for the Stellantis connected-car API (Opel, Peugeot, Citroën, DS).

The cars report charge level, range, odometer, plug and charge state and
their last position to the brand's cloud; the brand app shows them, and the
same API answers jolt with the owner's OAuth tokens. The login cannot be
automated: the identity service shows a captcha and redirects to the brand
app's own URL scheme. tools/stellantis_login.py walks the owner through it
once and stores the tokens in `cloud_credentials`; from then on this client
refreshes them. Each refresh returns a new refresh token and invalidates the
old one, so it is stored before the access token is used.

Ported from evcc (https://github.com/evcc-io/evcc, vehicle/psa and
cmd/token_psa.go), MIT license, Copyright (c) evcc.io. The status fields
follow flobz/psa_car_controller, whose recorded responses show the position
as GeoJSON, [longitude, latitude, altitude].

Nothing here logs or raises tokens, the account or a full VIN.
"""
import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from .. import models
from ..version import VERSION
from . import SourceError, credentials, mask
from .sessions import MIN_DRIVE_KM
from .snapshot import Snapshot, parse_time

SOURCE = "stellantis"
API = "https://api.groupe-psa.com/connectedcar/v4"
TIMEOUT_S = 15
# Refresh this long before the access token runs out, not after a 401.
REFRESH_MARGIN = timedelta(minutes=2)
USER_AGENT = f"jolt/{VERSION} (+https://github.com/cali1205/jolt)"
LOGIN_AGAIN = ("log in again: docker compose run --rm --no-deps -it jolt-app "
               "python tools/stellantis_login.py")
MAINTENANCE_EVERY = timedelta(hours=24)
# The backfill follows `next` this often at most: 60 trips a page, so years.
MAX_TRIP_PAGES = 200
# A trip's energy above this is not believed (a unit the scale did not catch).
MAX_KWH_PER_100KM = 80.0


@dataclass(frozen=True)
class Brand:
    realm: str
    client_id: str
    client_secret: str
    idp: str
    scheme: str

    @property
    def authorize_url(self) -> str:
        return self.idp + "/am/oauth2/authorize"

    @property
    def token_url(self) -> str:
        return self.idp + "/am/oauth2/access_token"

    def redirect_uri(self, country: str) -> str:
        return f"{self.scheme}://oauth2redirect/{country.lower()}"


# evcc's vehicle/psa/oauth2.go and vehicle/psa.go: the brand apps' clients.
BRANDS = {
    "opel": Brand("clientsB2COpel", "07364655-93cb-4194-8158-6b035ac2c24c",
                  "F2kK7lC5kF5qN7tM0wT8kE3cW1dP0wC5pI6vC0sQ5iP5cN8cJ8",
                  "https://idpcvs.opel.com", "mymopsdk"),
    "peugeot": Brand("clientsB2CPeugeot", "1eebc2d5-5df3-459b-a624-20abfcf82530",
                     "T5tP7iS0cO8sC0lA2iE2aR7gK6uE5rF3lJ8pC3nO1pR7tL8vU1",
                     "https://idpcvs.peugeot.com", "mymap"),
    "citroen": Brand("clientsB2CCitroen", "5364defc-80e6-447b-bec6-4af8d1542cae",
                     "iE0cD8bB0yJ0dS6rO3nN1hI2wU7uA5xR4gP7lD6vM0oH0nS8dN",
                     "https://idpcvs.citroen.com", "mymacsdk"),
    "ds": Brand("clientsB2CDS", "cbf74ee7-a303-4c3d-aba3-29f5994e2dfa",
                "X6bE6yQ3tH1cG5oA6aW4fS6hK0cR0aK5yN2wE4hP8vL8oW5gU3",
                "https://idpcvs.driveds.com", "mymdssdk"),
}

PROVIDES = frozenset({"soc_pct", "range_km", "odometer_km", "plugged", "charge_state",
                      "charge_type", "charge_remaining_min", "battery_health_pct",
                      "climate_state", "lat", "lon", "position_at", "outside_temp_c",
                      "ignition", "alarm_active", "aux_battery_pct",
                      "battery_resistance_pct", "capacity_kwh", "energy_kwh",
                      "maintenance_km", "maintenance_days"})

# A measurement this much older than the report is the car's last reading
# before it fell asleep: a Corsa-e parked since 13:27 still reported 15.5 °C
# at five in the morning, and its `kinetic.speed` was five months old
# (October 2026).
STALE = timedelta(minutes=30)
# Several cars report the battery's energy in a unit 1.343 times smaller than
# a watt hour (Home Assistant integration, issue #272: e-208, Mokka-e, ë-C4
# and Corsa-e against OBD readings; their KWH_CORRECTION). A Corsa-e with
# 49 kWh usable reports a capacity of 36608, and its trips 381 per percent of
# charge level over 650 km; times 1.343 that is 49.2 kWh and 512 Wh.
KWH_CORRECTION = 1.343
# Scaled, the capacity must lie this close to the profile's usable capacity
# times the state of health; otherwise no energy figure is believed.
SCALE_TOLERANCE = 0.15
# The residual reads some points below the charge level (the capacity holds
# energy below 0 %: 54.6 % of it at a displayed 60 %); further off it is not
# the same battery state.
RESIDUAL_TOLERANCE_PCT = 10.0
IGNITION = {"startup": "on", "start": "on", "stop": "off"}
ALARM = {"active": True, "inactive": False}


class StellantisError(SourceError):
    """Something the owner can act on; the text is safe to log and show."""


class LoginNeeded(StellantisError):
    pass


def account_key(brand: str, user: str) -> str:
    """The `cloud_credentials` account: tokens belong to one brand's client."""
    return f"{brand}:{user.strip().lower()}"


# ---------------------------------------------------------------------------
# Status -> snapshot (pure)
# ---------------------------------------------------------------------------

def _number(value) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _iso(moment: datetime | None) -> str | None:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def duration_min(text) -> float | None:
    """An ISO 8601 duration ("PT1H30M") in minutes, or None."""
    match = DURATION.match(str(text or "").strip())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return round(days * 1440 + hours * 60 + minutes + seconds / 60, 1)


def charge_state(status, plugged: bool | None) -> str | None:
    """The car's charging status as the catalog's `charge_state`."""
    status = str(status or "").strip().lower()
    if status == "inprogress":
        return "charging"
    if status == "failure":
        return "error"
    if status == "finished":
        return "purpose_reached"
    if plugged is None:
        return None
    return "ready" if plugged else "not_ready"


CHARGE_TYPES = {"slow": "AC", "quick": "DC"}


def _flatten(node, path: str, out: dict) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key != "_links":
                _flatten(value, f"{path}.{key}" if path else key, out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _flatten(value, f"{path}[{i}]", out)
    else:
        out[path] = [path, node, None]


def _dict(node) -> dict:
    return node if isinstance(node, dict) else {}


def _electric(doc: dict) -> tuple[dict, dict, dict]:
    """The electric energy entry, its charging and its battery part.
    `energies[]` is the newer shape and keeps both under `extension`;
    `energy[]` carries the same data and is the fallback."""
    for key in ("energies", "energy"):
        entry = next((e for e in doc.get(key) or []
                      if isinstance(e, dict) and e.get("type") == "Electric"), None)
        if entry:
            extension = _dict(_dict(entry.get("extension")).get("electric"))
            return (entry, _dict(extension.get("charging") or entry.get("charging")),
                    _dict(extension.get("battery") or entry.get("battery")))
    return {}, {}, {}


def _measured_recently(node: dict, captured_at: datetime) -> bool:
    """False for a measurement taken long before the report (`STALE`)."""
    taken = parse_time(node.get("createdAt") or node.get("updatedAt"))
    return taken is None or taken >= captured_at - STALE


def _captured_at(doc: dict) -> datetime | None:
    entry, _, _ = _electric(doc)
    return (parse_time(entry.get("updatedAt")) or parse_time(entry.get("createdAt"))
            or parse_time(doc.get("updatedAt")) or parse_time(doc.get("createdAt")))


def energy_scale(capacity, net_kwh: float | None,
                 health_pct: float | None = None) -> float | None:
    """The factor from the car's energy figures to watt hours: 1 or
    `KWH_CORRECTION`, whichever puts the reported capacity next to the
    profile's usable capacity aged by the state of health. None when neither
    does, or without a profile to compare with."""
    capacity = _number(capacity)
    if not capacity or capacity <= 0 or not net_kwh:
        return None
    expected = net_kwh * 1000 * ((health_pct or 100) / 100)
    scale = min((1.0, KWH_CORRECTION), key=lambda s: abs(capacity * s / expected - 1))
    return scale if abs(capacity * scale / expected - 1) <= SCALE_TOLERANCE else None


def status_scale(doc: dict, net_kwh: float | None) -> float | None:
    """`energy_scale` for one status answer."""
    _, _, battery = _electric(doc)
    health = _number(_dict(battery.get("health")).get("capacity"))
    return energy_scale(_dict(battery.get("load")).get("capacity"), net_kwh,
                        health if health and 0 < health <= 110 else None)


def _energy(battery: dict, scale: float | None, level) -> dict:
    load = _dict(battery.get("load"))
    capacity, residual = _number(load.get("capacity")), _number(load.get("residual"))
    if not scale or not capacity or capacity <= 0:
        return {}
    values = {"capacity_kwh": round(capacity * scale / 1000, 2)}
    if (residual is not None and 0 <= residual <= capacity and level is not None
            and abs(residual / capacity * 100 - level) <= RESIDUAL_TOLERANCE_PCT):
        values["energy_kwh"] = round(residual * scale / 1000, 2)
    return values


def _condition(doc: dict, captured_at: datetime) -> dict:
    """Ignition, alarm, outside temperature and the 12 V battery."""
    values: dict = {}
    ignition = IGNITION.get(str(_dict(doc.get("ignition")).get("type") or "").lower())
    if ignition:
        values["ignition"] = ignition
    alarm = ALARM.get(str(_dict(_dict(doc.get("alarm")).get("status"))
                          .get("activation") or "").lower())
    if alarm is not None:
        values["alarm_active"] = alarm
    air = _dict(_dict(doc.get("environment")).get("air"))
    temp = _number(air.get("temp"))
    if temp is not None and -60 <= temp <= 70 and _measured_recently(air, captured_at):
        values["outside_temp_c"] = temp
    # `battery` at the top is the 12 V battery; its "voltage" is a charge
    # level in percent (Home Assistant: service_battery_voltage, unit %).
    service = _dict(doc.get("battery"))
    level = _number(service.get("voltage"))
    if level is not None and 0 <= level <= 100 and _measured_recently(service, captured_at):
        values["aux_battery_pct"] = level
    return values


def _point(feature: dict) -> tuple[float, float] | None:
    """(latitude, longitude) of a GeoJSON point; its coordinates are
    [longitude, latitude, altitude]."""
    coordinates = _dict(feature.get("geometry")).get("coordinates") or []
    if len(coordinates) < 2:
        return None
    lon, lat = _number(coordinates[0]), _number(coordinates[1])
    if (lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180
            or (lat, lon) == (0, 0)):
        return None
    return lat, lon


def parse_maintenance(doc: dict) -> dict:
    """The `/maintenance` answer as catalog values; negative when overdue."""
    values = {}
    for name, key in (("maintenance_km", "mileageBeforeMaintenance"),
                      ("maintenance_days", "daysBeforeMaintenance")):
        if (number := _number(doc.get(key))) is not None:
            values[name] = number
    return values


def parse_status(doc: dict, scale: float | None = None) -> Snapshot | None:
    """One `/status` answer as a snapshot of catalog values, or None when
    it holds nothing jolt knows. `scale` (`status_scale`) turns the battery's
    energy figures into kWh; without it they are left out.

    Measurements (outside temperature, 12 V battery) count only when taken
    around the report (`STALE`). States count however old their time stamp:
    the ignition's is when it last changed, the odometer's when the car last
    moved. `kinetic` (speed, moving) is not read: it stops updating."""
    values: dict = {}
    electric, charging, battery = _electric(doc)
    captured_at = _captured_at(doc)
    if (level := _number(electric.get("level"))) is not None and 0 <= level <= 100:
        values["soc_pct"] = level
    if (autonomy := _number(electric.get("autonomy"))) is not None and autonomy >= 0:
        values["range_km"] = autonomy
    plugged = charging.get("plugged") if isinstance(charging.get("plugged"), bool) else None
    if plugged is not None:
        values["plugged"] = plugged
    state = charge_state(charging.get("status"), plugged)
    if state is not None:
        values["charge_state"] = state
    kind = CHARGE_TYPES.get(str(charging.get("chargingMode") or "").lower())
    if kind and state == "charging":
        values["charge_type"] = kind
    remaining = duration_min(charging.get("remainingTime"))
    if remaining is not None and state == "charging":
        values["charge_remaining_min"] = remaining
    health = _dict(battery.get("health"))
    if (capacity := _number(health.get("capacity"))) is not None and 0 < capacity <= 110:
        values["battery_health_pct"] = capacity
    if (resistance := _number(health.get("resistance"))) is not None and 0 < resistance <= 300:
        values["battery_resistance_pct"] = resistance
    values.update(_energy(battery, scale, values.get("soc_pct")))
    mileage = _number((doc.get("odometer") or {}).get("mileage"))
    if mileage is not None and mileage > 0:
        values["odometer_km"] = mileage
    position = _dict(doc.get("lastPosition"))
    if point := _point(position):
        values["lat"], values["lon"] = point
        properties = _dict(position.get("properties"))
        fixed = parse_time(properties.get("updatedAt") or properties.get("createdAt"))
        if fixed:
            values["position_at"] = _iso(fixed)
    preconditioning = _dict(doc.get("preconditioning") or doc.get("preconditionning"))
    climate = _dict(preconditioning.get("airConditioning")).get("status")
    if isinstance(climate, str) and climate.strip():
        values["climate_state"] = climate.strip().lower()
    if not values or captured_at is None:
        return None
    values.update(_condition(doc, captured_at))
    raw: dict = {}
    _flatten(doc, "", raw)
    return Snapshot(captured_at=captured_at, values=values, raw=raw)


# ---------------------------------------------------------------------------
# Trips -> logbook drives (pure)
# ---------------------------------------------------------------------------

def _naive_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _electric_of(entries) -> dict:
    return next((e for e in entries or [] if isinstance(e, dict)
                 and e.get("type") == "Electric"), {})


def _level(entries) -> float | None:
    level = _number(_electric_of(entries).get("level"))
    return level if level is not None and 0 <= level <= 100 else None


def trip_record(trip: dict, scale: float | None) -> dict | None:
    """One finished trip of `/trips` as a logbook drive record
    (cloud/logbook.py); None for one still running, one shorter than
    `sessions.MIN_DRIVE_KM` or one without times or mileage.

    `consumption` is the trip's energy and `avgConsumption` the same per
    100 km (consumption / distance * 100 on all 161 trips of a Corsa-e that
    have both), both in the unit of the status' energy figures, so `scale`
    turns them into kWh. `avgSpeed` is in m/s (distance / duration * 3.6 on
    the same trips, and Home Assistant converts it so); `maxSpeed` in km/h
    and 0 when the car did not measure it."""
    if trip.get("done") is not True or not trip.get("id"):
        return None
    start, end = parse_time(trip.get("startedAt")), parse_time(trip.get("stoppedAt"))
    odo, km = _number(trip.get("startMileage")), _number(trip.get("distance"))
    if start is None or end is None or end < start or odo is None or km is None:
        return None
    if km < MIN_DRIVE_KM:
        return None
    consumption = _number(_electric_of(trip.get("energyConsumptions")).get("consumption"))
    # 0 over kilometres comes with a "DataLacking" fault: the car lost the count.
    used = (round(consumption * scale / 1000, 2)
            if scale and consumption is not None and consumption > 0 else None)
    if used is not None and used / km * 100 > MAX_KWH_PER_100KM:
        used = None
    kinetic = _dict(trip.get("kinetic"))
    avg, top = _number(kinetic.get("avgSpeed")), _number(kinetic.get("maxSpeed"))
    soc_start = _level(trip.get("startEnergies"))
    soc_end = _level(trip.get("endEnergies"))
    started, ended = _naive_iso(start), _naive_iso(end)
    odo_end = round(odo + km, 1)
    begin = _point(_dict(trip.get("startPosition")))
    finish = _point(_dict(trip.get("stopPosition")))
    return {"started_at": started, "ended_at": ended,
            "odo_start": odo, "odo_end": odo_end, "distance_km": round(km, 1),
            "soc_start": soc_start, "soc_end": soc_end, "energy_used_kwh": used,
            "km_track": [[started, odo], [ended, odo_end]],
            "soc_track": ([[started, soc_start], [ended, soc_end]]
                          if soc_start is not None and soc_end is not None else []),
            "live_session_id": None,
            "trip_id": str(trip["id"]),
            "avg_speed_kmh": round(avg * 3.6, 1) if avg and avg > 0 else None,
            "max_speed_kmh": top if top and top > 0 else None,
            "start_lat": begin[0] if begin else None,
            "start_lon": begin[1] if begin else None,
            "end_lat": finish[0] if finish else None,
            "end_lon": finish[1] if finish else None}


def _page_token(page: dict, rel: str) -> str | None:
    href = _dict(_dict(page.get("_links")).get(rel)).get("href") or ""
    return (parse_qs(urlsplit(href).query).get("pageToken") or [None])[0]


def _trips_of(page: dict) -> list[dict]:
    return [t for t in _dict(page.get("_embedded")).get("trips") or [] if isinstance(t, dict)]


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge), as golang.org/x/oauth2 makes them."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(brand: str, country: str, challenge: str) -> str:
    b = BRANDS[brand]
    return b.authorize_url + "?" + urlencode({
        "client_id": b.client_id, "redirect_uri": b.redirect_uri(country),
        "response_type": "code", "scope": "openid profile",
        "code_challenge": challenge, "code_challenge_method": "S256"})


def _session(adapter=None) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    if adapter is not None:
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


def _token_request(session: requests.Session, brand: str, form: dict) -> dict:
    """POST to the brand's token endpoint; the new tokens with `expires_at`."""
    b = BRANDS[brand]
    try:
        response = session.post(b.token_url, data=form, auth=(b.client_id, b.client_secret),
                                headers={"Accept": "application/json"}, timeout=TIMEOUT_S)
    except requests.RequestException as failure:
        raise StellantisError(f"token: {type(failure).__name__}") from None
    if response.status_code in (400, 401):
        try:
            error = str(response.json().get("error") or "")[:40]
        except ValueError:
            error = ""
        raise LoginNeeded(f"token refused ({error or response.status_code}) - {LOGIN_AGAIN}")
    if response.status_code != 200:
        raise StellantisError(f"token: HTTP {response.status_code}")
    try:
        body = response.json()
        access = str(body["access_token"])
    except (ValueError, KeyError, TypeError):
        raise StellantisError("token: unexpected answer") from None
    expires_in = body.get("expires_in")
    seconds = expires_in if isinstance(expires_in, (int, float)) else 3600
    tokens = {"access_token": access,
              "expires_at": (datetime.now(timezone.utc)
                             + timedelta(seconds=seconds)).isoformat()}
    if body.get("refresh_token"):
        tokens["refresh_token"] = str(body["refresh_token"])
    return tokens


def exchange_code(brand: str, country: str, code: str, verifier: str,
                  adapter=None) -> dict:
    """The tokens for an authorization code from the browser login."""
    tokens = _token_request(_session(adapter), brand, {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": BRANDS[brand].redirect_uri(country), "code_verifier": verifier})
    if "refresh_token" not in tokens:
        raise StellantisError("token: the answer has no refresh token")
    return {"brand": brand, "country": country.lower(), **tokens}


def _fresh(data: dict, now: datetime) -> bool:
    expires = parse_time(data.get("expires_at"))
    return bool(data.get("access_token")) and expires is not None and now < expires - REFRESH_MARGIN


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Stellantis:
    """One account of one brand; tokens in `cloud_credentials`."""

    def __init__(self, brand: str, user: str, db_factory, adapter=None,
                 vehicle_id: int | None = None):
        """`vehicle_id` is the jolt vehicle whose profile decides the unit of
        the energy figures (`energy_scale`)."""
        if brand not in BRANDS:
            raise StellantisError(f"unknown brand {brand!r} (opel, peugeot, citroen, ds)")
        self.brand = BRANDS[brand]
        self.brand_name = brand
        self.account = account_key(brand, user)
        self.db_factory = db_factory
        self.session = _session(adapter)
        self.vehicle_id = vehicle_id
        self._ids: dict[str, str] = {}
        # From the last status answer, for the trips fetched after it.
        self.scale: float | None = None
        self._maintenance: dict = {}
        self._maintenance_at: datetime | None = None

    def _token(self, stale: str | None = None) -> str:
        """A valid access token. `stale` is one the API just refused: refresh
        unless another process already replaced it."""
        def renew(data):
            if not data or not data.get("refresh_token"):
                raise LoginNeeded(f"no tokens stored - {LOGIN_AGAIN}")
            if data.get("access_token") != stale and _fresh(data, datetime.now(timezone.utc)):
                return None
            tokens = _token_request(self.session, self.brand_name, {
                "grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
            return {**data, **tokens}

        db = self.db_factory()
        try:
            return credentials.update(db, SOURCE, self.account, renew)["access_token"]
        finally:
            db.close()

    def _get(self, step: str, path: str, params: dict | None = None) -> dict:
        token = self._token()
        for attempt in (1, 2):
            try:
                response = self.session.get(
                    API + path, params={"client_id": self.brand.client_id, **(params or {})},
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/hal+json",
                             "X-Introspect-Realm": self.brand.realm},
                    timeout=TIMEOUT_S)
            except requests.RequestException as failure:
                raise StellantisError(f"{step}: {type(failure).__name__}") from None
            if response.status_code == 401 and attempt == 1:
                token = self._token(stale=token)
                continue
            if response.status_code != 200:
                raise StellantisError(f"{step}: HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError:
                raise StellantisError(f"{step}: no JSON") from None
        raise StellantisError(f"{step}: HTTP 401")

    def vehicles(self) -> list[str]:
        body = self._get("vehicles", "/user/vehicles")
        found = ((body.get("_embedded") or {}).get("vehicles") or []
                 if isinstance(body, dict) else [])
        self._ids = {str(v["vin"]): str(v["id"]) for v in found
                     if isinstance(v, dict) and v.get("vin") and v.get("id")}
        return list(self._ids)

    def _path(self, vin: str) -> str:
        if vin not in self._ids:
            self.vehicles()
        if vin not in self._ids:
            raise StellantisError(f"vehicle {mask(vin)} is not on the account")
        return f"/user/vehicles/{self._ids[vin]}"

    def _net_kwh(self) -> float | None:
        if self.vehicle_id is None:
            return None
        db = self.db_factory()
        try:
            vehicle = db.get(models.Vehicle, self.vehicle_id)
            return vehicle.battery_net_kwh if vehicle else None
        finally:
            db.close()

    def _maintenance_raw(self, vin: str) -> dict:
        """The `/maintenance` answer, asked once a day; a failure keeps the
        last one and does not fail the status."""
        now = datetime.now(timezone.utc)
        if self._maintenance_at is None or now - self._maintenance_at >= MAINTENANCE_EVERY:
            self._maintenance_at = now
            try:
                self._maintenance = self._get("maintenance", self._path(vin) + "/maintenance")
            except StellantisError:
                pass
        return self._maintenance if isinstance(self._maintenance, dict) else {}

    def latest(self, vin: str) -> Snapshot | None:
        doc = self._get("status", self._path(vin) + "/status")
        self.scale = status_scale(doc, self._net_kwh())
        snap = parse_status(doc, self.scale)
        if snap is None:
            return None
        maintenance = self._maintenance_raw(vin)
        raw: dict = {}
        _flatten(maintenance, "maintenance", raw)
        return Snapshot(captured_at=snap.captured_at,
                        values={**snap.values, **parse_maintenance(maintenance)},
                        raw={**snap.raw, **raw})

    def updates(self, vin: str) -> list[Snapshot]:
        """The current status; the poller stores it once per report time."""
        snap = self.latest(vin)
        return [snap] if snap else []

    def _trips_page(self, vin: str, token: str | None = None) -> dict:
        return self._get("trips", self._path(vin) + "/trips",
                         {"pageToken": token} if token else None)

    def trips(self, vin: str, everything: bool) -> list[dict]:
        """The car's trips, oldest first, as the API lists them: all pages
        (`everything`, following `next`), or else the last two, where a new
        trip appears and the one before it may have finished meanwhile."""
        first = self._trips_page(vin)
        if everything:
            pages, seen, page = [first], {_page_token(first, "self")}, first
            while ((token := _page_token(page, "next")) and token not in seen
                   and len(pages) < MAX_TRIP_PAGES):
                seen.add(token)
                page = self._trips_page(vin, token)
                pages.append(page)
            return [t for p in pages for t in _trips_of(p)]
        last_token = _page_token(first, "last")
        if not last_token or last_token == _page_token(first, "self"):
            return _trips_of(first)
        last = self._trips_page(vin, last_token)
        before = _page_token(last, "prev")
        if before and before != _page_token(first, "self"):
            first = self._trips_page(vin, before)
        return _trips_of(first) + _trips_of(last)

    def drives(self, vin: str, everything: bool) -> list[dict]:
        """`trips` as logbook drive records, energy in the unit the last
        status answer decided (`scale`)."""
        return [r for t in self.trips(vin, everything)
                if (r := trip_record(t, self.scale)) is not None]
