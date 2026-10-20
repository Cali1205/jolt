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
from urllib.parse import urlencode

import requests

from ..version import VERSION
from . import SourceError, credentials, mask
from .snapshot import Snapshot, parse_time

SOURCE = "stellantis"
API = "https://api.groupe-psa.com/connectedcar/v4"
TIMEOUT_S = 15
# Refresh this long before the access token runs out, not after a 401.
REFRESH_MARGIN = timedelta(minutes=2)
USER_AGENT = f"jolt/{VERSION} (+https://github.com/cali1205/jolt)"
LOGIN_AGAIN = ("log in again: docker compose run --rm --no-deps -it jolt-app "
               "python tools/stellantis_login.py")


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
                      "climate_state", "lat", "lon", "position_at"})


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


def parse_status(doc: dict) -> Snapshot | None:
    """One `/status` answer as a snapshot of catalog values, or None when
    it holds nothing jolt knows."""
    values: dict = {}
    electric = next((e for e in doc.get("energy") or []
                     if isinstance(e, dict) and e.get("type") == "Electric"), None) or {}
    if (level := _number(electric.get("level"))) is not None and 0 <= level <= 100:
        values["soc_pct"] = level
    if (autonomy := _number(electric.get("autonomy"))) is not None and autonomy >= 0:
        values["range_km"] = autonomy
    charging = electric.get("charging") or {}
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
    health = (((electric.get("battery") or {}).get("health") or {}).get("capacity")
              if isinstance(electric.get("battery"), dict) else None)
    if health is None:
        health = ((doc.get("battery") or {}).get("health") or {}).get("capacity")
    if (health := _number(health)) is not None and 0 < health <= 110:
        values["battery_health_pct"] = health
    mileage = _number((doc.get("odometer") or {}).get("mileage"))
    if mileage is not None and mileage > 0:
        values["odometer_km"] = mileage
    position = doc.get("lastPosition") or {}
    coordinates = (position.get("geometry") or {}).get("coordinates") or []
    if len(coordinates) >= 2:
        lon, lat = _number(coordinates[0]), _number(coordinates[1])
        if (lat is not None and lon is not None and -90 <= lat <= 90
                and -180 <= lon <= 180 and (lat, lon) != (0, 0)):
            values["lat"], values["lon"] = lat, lon
            properties = position.get("properties") or {}
            fixed = parse_time(properties.get("updatedAt") or properties.get("createdAt"))
            if fixed:
                values["position_at"] = _iso(fixed)
    climate = ((doc.get("preconditionning") or {}).get("airConditioning") or {}).get("status")
    if isinstance(climate, str) and climate.strip():
        values["climate_state"] = climate.strip().lower()
    if not values:
        return None
    captured_at = (parse_time(electric.get("updatedAt")) or parse_time(doc.get("updatedAt"))
                   or parse_time(doc.get("createdAt")))
    if captured_at is None:
        return None
    raw: dict = {}
    _flatten(doc, "", raw)
    return Snapshot(captured_at=captured_at, values=values, raw=raw)


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

    def __init__(self, brand: str, user: str, db_factory, adapter=None):
        if brand not in BRANDS:
            raise StellantisError(f"unknown brand {brand!r} (opel, peugeot, citroen, ds)")
        self.brand = BRANDS[brand]
        self.brand_name = brand
        self.account = account_key(brand, user)
        self.db_factory = db_factory
        self.session = _session(adapter)
        self._ids: dict[str, str] = {}

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

    def _get(self, step: str, path: str) -> dict:
        token = self._token()
        for attempt in (1, 2):
            try:
                response = self.session.get(
                    API + path, params={"client_id": self.brand.client_id},
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

    def latest(self, vin: str) -> Snapshot | None:
        if vin not in self._ids:
            self.vehicles()
        if vin not in self._ids:
            raise StellantisError(f"vehicle {mask(vin)} is not on the account")
        return parse_status(self._get("status", f"/user/vehicles/{self._ids[vin]}/status"))

    def updates(self, vin: str) -> list[Snapshot]:
        """The current status; the poller stores it once per report time."""
        snap = self.latest(vin)
        return [snap] if snap else []
