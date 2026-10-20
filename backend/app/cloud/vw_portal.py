"""Client for the VW group EU Data Act portal.

Since May 2026 VW no longer lets third parties use the WeConnect app API. What
remains is the portal VW runs for the EU Data Act: log in with the VW ID, and
once the owner has switched on a "continuous data request" in the browser,
the portal keeps a zipped JSON dataset with the car's last reported values
roughly every 15 minutes. This is read-only and only as fresh as the car's
last report.

Ported from evcc (https://github.com/evcc-io/evcc, vehicle/vw/eudataact and
vehicle/vag/vwidentity), MIT license, Copyright (c) evcc.io. The login
follows evcc's flow step by step, including where it stops following
redirects; the `Referer` headers and the honest User-Agent follow
TA2k/ioBroker.vw-connect (lib/euDataAct.js). There is deliberately no request
to the portal's root page: the portal operator asked clients to stop priming
it (evcc PR #34042).

Nothing here logs or raises the user name, the password, cookies or a full
VIN. Error texts carry the step and the HTTP status, never a URL with a VIN
or a query string from the identity service.
"""
import io
import os
import json
import random
import re
import string
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import requests

from ..version import VERSION
from . import SourceError, mask, snapshot

PORTAL = "https://eu-data-act.drivesomethinggreater.com"
PORTAL_HOST = "eu-data-act.drivesomethinggreater.com"
IDENTITY = "https://identity.vwgroup.io"
IDENTITY_HOST = "identity.vwgroup.io"
AUTHORIZE_URL = IDENTITY + "/oidc/v1/authorize"
REDIRECT_URI = PORTAL + "/login"
SCOPE = "openid cars profile"

TIMEOUT_S = 15
MAX_REDIRECTS = 10
# Datasets downloaded on the first poll to seed the merged state.
MAX_BACKFILL = 8
# A dataset is a few kilobytes; anything far larger is not one.
MAX_DATASET_BYTES = 20 * 1024 * 1024
# VW's firewall in front of the portal decides by headers we cannot test
# from here. VW_PORTAL_USER_AGENT replaces this one without a new image.
USER_AGENT = (os.environ.get("VW_PORTAL_USER_AGENT", "").strip()
              or f"jolt/{VERSION} (+https://github.com/cali1205/jolt)")

REDIRECT_STATUS = (301, 302, 303, 307, 308)
JSON = {"Accept": "application/json"}

# (OIDC client id, state suffix) per brand; all brands share one portal.
BRANDS = {
    "volkswagen": ("9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com",
                   "VOLKSWAGEN_PASSENGER_CARS"),
    "audi": ("cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com", "AUDI"),
    "skoda": ("3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com", "SKODA"),
    "seat": ("f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com", "SEAT"),
    "cupra": ("f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com", "CUPRA"),
}


class PortalError(SourceError):
    pass


class StatusError(PortalError):
    def __init__(self, step: str, status: int):
        super().__init__(f"{step}: HTTP {status}")
        self.status = status


# ---------------------------------------------------------------------------
# Login pages
# ---------------------------------------------------------------------------

@dataclass
class Form:
    action: str
    inputs: dict[str, str]


class _FormParser(HTMLParser):
    def __init__(self, form_id: str):
        super().__init__()
        self.form_id = form_id
        self.form: Form | None = None
        self.inside = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and self.form is None and attrs.get("id") == self.form_id:
            self.form = Form(action=attrs.get("action") or "", inputs={})
            self.inside = True
        elif tag == "input" and self.inside and attrs.get("name"):
            self.form.inputs[attrs["name"]] = attrs.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


def parse_form(html: str, form_id: str) -> Form | None:
    parser = _FormParser(form_id)
    parser.feed(html)
    return parser.form


def parse_credentials(html: str) -> dict:
    """The `window._IDK` object of the password page - a JavaScript literal,
    turned into JSON the way evcc does it (vwidentity/forms.go)."""
    match = re.search(r"window\._IDK\s*=\s*(.*?)[;<]", html, re.S)
    if not match:
        raise PortalError("credentials page: IDK form not found")
    text = match.group(1).replace("'", '"')
    text = re.sub(r"\s(\w+):", r' "\1":', text)
    text = re.sub(r",\s+}", "}", text)
    try:
        return json.loads(text)
    except ValueError as failure:
        raise PortalError("credentials page: cannot read IDK form") from failure


def _netloc(url: str) -> str:
    return urlsplit(url).netloc.lower()


def is_login_host(url: str) -> bool:
    return _netloc(url) in (IDENTITY_HOST, PORTAL_HOST)


def is_user_page(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.netloc.lower() == PORTAL_HOST
            and parts.path.startswith("/content/euda/")
            and parts.path.endswith("/user.html"))


def is_marketing_page(url: str) -> bool:
    return (_netloc(url) == IDENTITY_HOST
            and "/consent/marketing/" in urlsplit(url).path)


def marketing_callback(url: str) -> str | None:
    """The OIDC callback embedded in VW's optional marketing consent page.

    Following it continues the login without consenting.
    """
    parts = urlsplit(url)
    if "/consent/marketing/" not in parts.path:
        return None
    callback = dict(parse_qsl(parts.query)).get("callback")
    if not callback:
        raise PortalError("marketing consent page is missing callback url")
    cb = urlsplit(callback)
    # Spaces in the scopes would make the request invalid.
    return cb._replace(query=urlencode(parse_qsl(cb.query))).geturl()


def valid_marketing_callback(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.netloc.lower() == IDENTITY_HOST
            and parts.path in ("/oidc/v1/oauth/client/callback",
                               "/oidc/v1/oauth/client/callback/success"))


def _where(url: str) -> str:
    """A URL without its query: identity URLs carry user ids and HMACs."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Dataset:
    name: str
    created: datetime


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _items(data, key: str) -> list:
    """The portal answers either with a bare list or with {key: [...]}."""
    if isinstance(data, list):
        return data
    return (data.get(key) or []) if isinstance(data, dict) else []


def parse_datasets(data) -> list[Dataset]:
    items = _items(data, "files")
    return [Dataset(name=str(d.get("name") or ""),
                    created=snapshot.parse_time(d.get("createdOn")) or EPOCH)
            for d in items if isinstance(d, dict)]


def pending(listing: list[Dataset], after: datetime | None) -> list[Dataset]:
    """Datasets still to download, oldest first.

    The portal lists "..._no_content_found.zip" placeholders while the car
    sleeps; those carry nothing. On the first poll only the latest
    MAX_BACKFILL are fetched, afterwards only what came after the newest one
    already merged, so no dataset is downloaded twice.
    """
    content = sorted((d for d in listing
                      if not d.name.lower().endswith("_no_content_found.zip")),
                     key=lambda d: d.created)
    if after is None:
        return content[-MAX_BACKFILL:]
    return [d for d in content if d.created > after]


def parse_dataset(blob: bytes) -> list[snapshot.Point]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
        member = next((m for m in archive.infolist()
                       if m.filename.lower().endswith(".json")), None)
        if member is None:
            raise PortalError("dataset: no json document")
        if member.file_size > MAX_DATASET_BYTES:
            raise PortalError("dataset: too large")
        document = json.loads(archive.read(member))
    except (zipfile.BadZipFile, ValueError) as failure:
        raise PortalError("dataset: unreadable") from failure
    return snapshot.decode(document.get("Data") or [])


@dataclass
class _VinState:
    identifier: str = ""
    after: datetime | None = None
    seq: int = 0
    points: list[snapshot.Point] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class VwPortal:
    """One portal account. Not thread-safe: one caller at a time."""

    def __init__(self, user: str, password: str, brand: str = "Volkswagen",
                 adapter: requests.adapters.BaseAdapter | None = None):
        if brand.lower() not in BRANDS:
            raise ValueError(f"unknown brand {brand!r}, known: "
                             + ", ".join(sorted(BRANDS)))
        self._user = user
        self._password = password
        self._client_id, self._state = BRANDS[brand.lower()]
        # Tests route all traffic through a fake portal with this.
        self._adapter = adapter
        self._session: requests.Session | None = None
        self._vins: dict[str, _VinState] = {}

    def __repr__(self) -> str:
        return "VwPortal()"

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        if self._adapter is not None:
            session.mount("https://", self._adapter)
            session.mount("http://", self._adapter)
        return session

    # ----- login -----

    def _request(self, session, step, method, url, **kwargs) -> requests.Response:
        try:
            return session.request(method, url, allow_redirects=False,
                                   timeout=TIMEOUT_S, **kwargs)
        except requests.RequestException as failure:
            # The exception text contains the URL, and data URLs contain
            # the VIN.
            raise PortalError(f"{step}: network error "
                              f"({type(failure).__name__})") from None

    def _follow(self, session, step, method, url, data=None,
                headers=None) -> requests.Response:
        """One login step with evcc's redirect policy: stay on the identity
        service and the portal, and stop at the portal's user page or at the
        marketing consent page without fetching them."""
        hops = 0
        while True:
            response = self._request(session, step, method, url, data=data,
                                     headers=headers)
            location = response.headers.get("Location")
            if response.status_code not in REDIRECT_STATUS or not location:
                return response
            target = urljoin(url, location)
            if (urlsplit(target).scheme != "https" or is_user_page(target)
                    or is_marketing_page(target)):
                return response
            if not is_login_host(target):
                raise PortalError(f"{step}: unexpected login redirect host "
                                  f"{_netloc(target)}")
            hops += 1
            if hops >= MAX_REDIRECTS:
                raise PortalError(f"{step}: stopped after {MAX_REDIRECTS} redirects")
            if response.status_code not in (307, 308):
                method, data = "GET", None
            url = target

    @staticmethod
    def _landing(response: requests.Response) -> str:
        if response.status_code >= 400:
            raise StatusError("login", response.status_code)
        if response.status_code in REDIRECT_STATUS:
            location = response.headers.get("Location")
            if not location:
                raise PortalError("login redirect: no Location header")
            return urljoin(response.url, location)
        return response.url

    def _login(self) -> None:
        session = self._new_session()
        query = {
            "client_id": self._client_id,
            "response_type": "code",
            "scope": SCOPE,
            "state": f"de__en__{self._state}",
            "redirect_uri": REDIRECT_URI,
            "prompt": "login",
            "nonce": "".join(random.choices(string.ascii_letters, k=43)),
        }
        signin = self._follow(session, "authorize", "GET",
                              AUTHORIZE_URL + "?" + urlencode(query))
        if signin.status_code >= 400:
            raise StatusError("authorize", signin.status_code)
        form = parse_form(signin.text, "emailPasswordForm")
        if form is None:
            raise PortalError("identifier form not found "
                              "(portal layout may have changed)")
        identifier_url = urljoin(IDENTITY, form.action)
        if _netloc(identifier_url) != IDENTITY_HOST:
            raise PortalError("identifier form posts to an unexpected host")

        identifier = self._follow(
            session, "identifier", "POST", identifier_url,
            data={"_csrf": form.inputs.get("_csrf", ""),
                  "relayState": form.inputs.get("relayState", ""),
                  "hmac": form.inputs.get("hmac", ""),
                  "email": self._user},
            headers={"Referer": signin.url})
        if identifier.status_code >= 400:
            raise StatusError("identifier", identifier.status_code)
        params = parse_credentials(identifier.text)
        model = params.get("templateModel") or {}
        if model.get("error"):
            raise PortalError(f"login rejected: {model['error']}")
        old, new = model.get("identifierUrl"), model.get("postAction")
        if not old or not new or old not in identifier_url:
            raise PortalError("credentials page: post action missing")

        landing = self._follow(
            session, "authenticate", "POST",
            identifier_url.replace(old, new),
            data={"_csrf": params.get("csrf_token", ""),
                  "relayState": model.get("relayState", ""),
                  "hmac": model.get("hmac", ""),
                  "email": self._user,
                  "password": self._password},
            headers={"Referer": identifier.url})
        final = self._landing(landing)

        callback = marketing_callback(final)
        if callback is not None:
            if (urlsplit(final).scheme != "https" or not is_login_host(final)
                    or not valid_marketing_callback(callback)):
                raise PortalError("unexpected marketing consent callback URL")
            final = self._landing(self._follow(
                session, "marketing consent", "GET", callback,
                headers={"Referer": final}))

        path = urlsplit(final).path
        if "signin-service" in path or "/consent" in path or "/error" in path:
            raise PortalError("login did not complete - open the portal in a "
                              "browser and confirm the consent: " + _where(final))
        if _netloc(final) != PORTAL_HOST:
            raise PortalError(f"login did not complete: unexpected landing "
                              f"host {_netloc(final)}")
        if not is_user_page(final):
            raise PortalError("login did not complete: unexpected landing page")
        self._session = session

    # ----- data -----

    def _get(self, step: str, url: str, headers: dict) -> requests.Response:
        """GET with the portal session; logs in again once if it expired.

        Redirects are not followed: on a data call one only means the
        session is gone, and following it would leave the two known hosts.
        The portal sits behind Adobe AEM, which answers an expired session
        with a 500 HTML page instead of a 401 (ioBroker.vw-connect).
        """
        logged_in = False
        if self._session is None:
            self._login()
            logged_in = True
        while True:
            response = self._request(self._session, step, "GET", url,
                                     headers=headers)
            status = response.status_code
            expired = (status in (401, 403) or 300 <= status < 400
                       or (status >= 500
                           and response.text.lstrip().startswith("<")))
            if expired and not logged_in:
                self._login()
                logged_in = True
                continue
            if status >= 300:
                raise StatusError(step, status)
            return response

    @staticmethod
    def _json(response: requests.Response, step: str):
        try:
            return response.json()
        except ValueError:
            raise PortalError(f"{step}: no JSON in response") from None

    def vehicles(self) -> list[str]:
        """The VINs linked to the account in the portal."""
        response = self._get("vehicles",
                             PORTAL + "/proxy_api/consent/me/vehicles"
                             "?viewPosition=FRONT_LEFT", JSON)
        data = self._json(response, "vehicles")
        vins = [str(v.get("vin") or v.get("vehicleIdentificationNumber") or "")
                for v in _items(data, "vehicles") if isinstance(v, dict)]
        return [v for v in vins if v]

    def _identifier(self, vin: str) -> str:
        url = (f"{PORTAL}/proxy_api/euda-apim/datarequest/vehicles/"
               f"{quote(vin, safe='')}/metadata/partial")
        try:
            response = self._get("data request", url, JSON)
        except StatusError as failure:
            if failure.status == 404:
                raise PortalError(
                    f"no data request for vehicle {mask(vin)} - enable the "
                    "continuous data request in the portal") from None
            raise
        data = self._json(response, "data request")
        identifier = data.get("Identifier") if isinstance(data, dict) else None
        if not identifier:
            raise PortalError(f"no data request for vehicle {mask(vin)}")
        return str(identifier)

    def _base(self, vin: str, identifier: str) -> str:
        return (f"{PORTAL}/proxy_api/euda-apim/datadelivery/vehicles/"
                f"{quote(vin, safe='')}/{quote(identifier, safe='')}")

    def _datasets(self, vin: str, identifier: str) -> list[Dataset]:
        try:
            response = self._get("dataset list", self._base(vin, identifier)
                                 + "/list", {**JSON, "type": "partial"})
        except StatusError as failure:
            # 404 "No files available" until the car delivered its first one.
            if failure.status == 404:
                return []
            raise
        return parse_datasets(self._json(response, "dataset list"))

    def _download(self, vin: str, identifier: str, name: str) -> bytes:
        return self._get("dataset download", self._base(vin, identifier)
                         + "/download",
                         {"filename": name, "type": "partial"}).content

    def updates(self, vin: str) -> list[snapshot.Snapshot]:
        """One snapshot per report that is new since the last call, oldest
        first. Each report is a moment of the car (parked, driving, parked
        again); folding them into one would keep only the last moment, and
        a drive between two polls would vanish."""
        state = self._vins.setdefault(vin, _VinState())
        if not state.identifier:
            state.identifier = self._identifier(vin)
        found = []
        for dataset in pending(self._datasets(vin, state.identifier), state.after):
            points = parse_dataset(self._download(vin, state.identifier,
                                                  dataset.name))
            if state.after is None or dataset.created > state.after:
                state.after = dataset.created
            state.seq += 1
            state.points = snapshot.merge(state.points, points, state.seq)
            snap = snapshot.from_points(state.points, points)
            if snap is not None:
                found.append(snap)
        return found

    def latest(self, vin: str) -> snapshot.Snapshot | None:
        """Fetch what is new for the car and return the merged snapshot."""
        self.updates(vin)
        return snapshot.from_points(self._vins[vin].points)

    def points(self, vin: str) -> list[snapshot.Point]:
        """The merged raw points behind the last `latest(vin)`."""
        state = self._vins.get(vin)
        return list(state.points) if state else []
