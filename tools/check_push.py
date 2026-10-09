#!/usr/bin/env python3
"""Checks the notifications to the phone - Web Push.

What is **not** checked here, and why: the network hop to the push service
(Google, Mozilla, Apple). It does not belong in a check script - it needs a
real device with a real subscription, and a script that runs against
third-party services will eventually fail for reasons that have nothing to
do with jolt.

What is checked is everything before that - and that is most of it:

- **The keys.** Does jolt generate a pair that py_vapid accepts, and does
  the public one match the private one? A key in the wrong format otherwise
  only shows up on the phone.
- **The encryption, round trip.** The payload is encrypted for a
  reconstructed browser subscription and decrypted again with its private
  key. If the plaintext comes back, the whole path according to RFC 8291
  is correct - that is the part where Web Push fails in practice.
- **Subscription management.** Creating is idempotent, unsubscribing works.
- **Clean-up.** A subscription whose browser has unsubscribed (404/410)
  disappears. A timeout or a 500, however, does not - whoever throws away
  subscriptions at the first glitch permanently switches notifications off.
- **The trigger.** Without a key the function is off and claims nothing.

    ./tools/check_push.py
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("push")

import http_ece  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from py_vapid import Vapid01  # noqa: E402
from pywebpush import WebPusher  # noqa: E402

from app import models, push  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

verify = Check()


def b64(raw_data: bytes) -> str:
    return base64.urlsafe_b64encode(raw_data).rstrip(b"=").decode("ascii")


class Browserabo:
    """A reconstructed subscription, as a browser would create it.

    It has a real private key - only that makes it possible to check whether
    the encrypted payload becomes readable again at the recipient.
    """

    def __init__(self, endpoint: str = "https://push.example.org/abo-1"):
        self.endpoint = endpoint
        self.private_ = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.p256dh = self.private_.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)

    def as_json(self) -> dict:
        return {"endpoint": self.endpoint, "p256dh": b64(self.p256dh),
                "auth": b64(self.auth), "device": "Prüf-Browser"}

    def decipher(self, content: bytes) -> dict:
        clear = http_ece.decrypt(content, private_key=self.private_,
                                auth_secret=self.auth, version="aes128gcm")
        return json.loads(clear.decode("utf-8"))


# ---------------------------------------------------------------------------

def part_key():
    print("\nVAPID-Schlüssel")
    private_, publicly = push.generate_key()
    raw_public_ = base64.urlsafe_b64decode(
        publicly + "=" * (-len(publicly) % 4))
    verify(len(raw_public_) == 65,
           "der öffentliche Schlüssel ist ein unkomprimierter Punkt (65 Byte) - "
           "genau das erwartet der Browser", f"{len(raw_public_)} Byte")
    verify(raw_public_[0] == 0x04,
           "und beginnt mit 0x04, wie es die Kodierung verlangt")

    # Does py_vapid accept the private key, and do both belong together?
    vapid = Vapid01.from_string(private_)
    derived = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    verify(b64(derived) == publicly,
           "der öffentliche Schlüssel gehört zum privaten - sonst nimmt kein "
           "Push-Dienst die Nachricht an")

    header_rows = vapid.sign({"aud": "https://push.example.org",
                             "sub": "mailto:jolt@example.org"})
    verify("Authorization" in header_rows
           and header_rows["Authorization"].startswith("WebPush "),
           "aus dem Schlüssel entsteht eine gültige Authorization-Kopfzeile",
           str(sorted(header_rows))[:80])


def part_encryption():
    print("\nNutzlast verschlüsseln und wieder lesen (RFC 8291)")
    browser = Browserabo()
    contents = json.dumps({"title": "jolt – Ladeplan geändert",
                         "text": "Nächster Stopp jetzt Rasthof Nord bei km 212.",
                         "url": "/"}, ensure_ascii=False).encode("utf-8")

    encrypted = WebPusher({"endpoint": browser.endpoint,
                                "keys": {"p256dh": b64(browser.p256dh),
                                         "auth": b64(browser.auth)}}
                               ).encode(contents, content_encoding="aes128gcm")
    content = encrypted["body"] if isinstance(encrypted, dict) \
        else encrypted
    verify(len(content) > len(contents),
           "die verschlüsselte Nachricht ist länger als der Klartext",
           f"{len(content)} statt {len(contents)} Byte")
    verify(contents not in content,
           "und der Klartext steht nicht mehr darin - der Push-Dienst kann "
           "nicht mitlesen")

    back = browser.decipher(content)
    verify(back["text"].startswith("Nächster Stopp"),
           "der Empfänger bekommt den Klartext zurück", str(back)[:70])
    verify("Ladeplan geändert" in back["title"],
           "auch Umlaute überstehen den Rundlauf", back["title"])


def part_subscriptions():
    print("\nAbos anlegen, auffrischen, abmelden")
    db = SessionLocal()
    try:
        db.query(models.PushSubscription).delete()
        db.commit()

        browser = Browserabo()
        records = browser.as_json()
        push.save_subscription(db, records["endpoint"], records["p256dh"],
                           records["auth"], records["device"])
        verify(db.query(models.PushSubscription).count() == 1, "ein Abo ist angelegt")

        push.save_subscription(db, records["endpoint"], records["p256dh"],
                           records["auth"], "Anderes Gerät")
        count = db.query(models.PushSubscription).count()
        verify(count == 1,
               "ein zweiter Aufruf legt nichts doppelt an - sonst käme jede "
               "Meldung mehrfach", f"{count} Abos")
        saved = db.query(models.PushSubscription).one()
        verify(saved.device == "Anderes Gerät",
               "aber er frischt das Abo auf", saved.device)

        verify(push.delete_subscription(db, records["endpoint"]) is True,
               "das Abo lässt sich abmelden")
        verify(db.query(models.PushSubscription).count() == 0, "und ist dann weg")
        verify(push.delete_subscription(db, "gibt-es-nicht") is False,
               "ein unbekanntes Abo abzumelden ist kein Fehler")
    finally:
        db.close()


def part_dispatch():
    print("\nVersand: wer bekommt was, und was passiert bei Fehlern")
    db = SessionLocal()
    try:
        db.query(models.PushSubscription).delete()
        db.commit()

        lebt = Browserabo("https://push.example.org/lebt")
        signed_out = Browserabo("https://push.example.org/abgemeldet")
        disturbed = Browserabo("https://push.example.org/gestoert")
        for browser in (lebt, signed_out, disturbed):
            d = browser.as_json()
            push.save_subscription(db, d["endpoint"], d["p256dh"], d["auth"])

        empfangen: dict[str, bytes] = {}

        def dispatcher(subscription, msg):
            empfangen[subscription.endpoint] = msg
            if subscription.endpoint.endswith("abgemeldet"):
                return 410      # the browser has revoked the permission
            if subscription.endpoint.endswith("gestoert"):
                return 500      # the push service has a problem right now
            return 201

        result = push.send(db, "jolt", "Ladeplan geändert", dispatcher=dispatcher)
        verify(result["sent"] == 1, "ein Gerät hat die Meldung bekommen",
               str(result))
        verify(result["removed"] == 1, "ein abgemeldetes Abo wurde entfernt",
               str(result))
        verify(result["failure"] == 1, "eine Störung wurde als Fehler gezählt",
               str(result))

        left = {a.endpoint for a in db.query(models.PushSubscription).all()}
        verify(signed_out.endpoint not in left,
               "das abgemeldete Gerät steht nicht mehr in der Datenbank")
        verify(disturbed.endpoint in left,
               "das gestörte dagegen schon - eine 500 sagt nichts über das Abo, "
               "und wer es wegwirft, schaltet Benachrichtigungen dauerhaft ab")

        contents = json.loads(empfangen[lebt.endpoint].decode("utf-8"))
        verify(contents["title"] == "jolt" and contents["text"] == "Ladeplan geändert",
               "die Nutzlast trägt Titel und Text", str(contents))
        verify("url" in contents,
               "und ein Ziel für den Klick auf die Meldung")

        # A sender that throws must not abort sending to the others - in a dead
        # zone this is the normal case.
        def raises(subscription, msg):
            if subscription.endpoint.endswith("lebt"):
                raise OSError("Netz weg")
            return 201

        result = push.send(db, "jolt", "zweiter Versuch", dispatcher=raises)
        verify(result["failure"] == 1 and result["sent"] == 1,
               "ein geworfener Fehler bricht den Versand an die anderen nicht ab",
               str(result))
    finally:
        db.close()


def part_without_key():
    print("\nOhne VAPID-Schlüssel")
    old = (os.environ.pop("VAPID_PRIVATE_KEY", None),
           os.environ.pop("VAPID_PUBLIC_KEY", None))
    try:
        verify(push.actual_configured() is False,
               "die Funktion meldet sich als nicht eingerichtet")

        db = SessionLocal()
        try:
            result = push.send(db, "jolt", "sollte nicht rausgehen")
        finally:
            db.close()
        verify(result.get("skipped") is True and result["sent"] == 0,
               "und es wird nichts verschickt - statt es vorzutäuschen",
               str(result))

        client = TestClient(app)
        response = client.get("/api/push/key").json()
        verify(response["configured"] is False,
               "die Oberfläche erfährt das über /api/push/key",
               str(response))

        created_at = client.post("/api/push/subscription", json={
            "endpoint": "https://push.example.org/x", "p256dh": "a" * 20,
            "auth": "b" * 10})
        verify(created_at.status_code == 409,
               "ein Abo anzulegen wird sauber abgelehnt, nicht still verschluckt",
               f"HTTP {created_at.status_code}")
    finally:
        for name, val in zip(("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY"), old):
            if val is not None:
                os.environ[name] = val


def part_with_key():
    print("\nMit VAPID-Schlüssel über die Endpunkte")
    private_, publicly = push.generate_key()
    os.environ["VAPID_PRIVATE_KEY"] = private_
    os.environ["VAPID_PUBLIC_KEY"] = publicly
    os.environ["VAPID_SUBJECT"] = "mailto:jolt@example.org"
    try:
        client = TestClient(app)
        # Without network every https name counts as public; the check itself is
        # in check_security.py.
        push.endpoint_allowed = lambda url: url.startswith("https://")
        response = client.get("/api/push/key").json()
        verify(response["configured"] is True, "eingerichtet")
        verify(response["public_key"] == publicly,
               "und der öffentliche Schlüssel kommt heraus")

        browser = Browserabo("https://push.example.org/ueber-api")
        created_at = client.post("/api/push/subscription", json=browser.as_json())
        verify(created_at.status_code == 200, "ein Abo lässt sich anlegen",
               f"HTTP {created_at.status_code}: {created_at.text[:100]}")

        db = SessionLocal()
        try:
            found = db.query(models.PushSubscription).filter_by(
                endpoint=browser.endpoint).one_or_none()
        finally:
            db.close()
        verify(found is not None, "und steht in der Datenbank")

        downhill = client.request("DELETE", "/api/push/subscription",
                            json={"endpoint": browser.endpoint})
        verify(downhill.status_code == 200 and downhill.json()["ok"] is True,
               "und wieder abmelden", f"HTTP {downhill.status_code}")
    finally:
        for name in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_SUBJECT"):
            os.environ.pop(name, None)


def main() -> int:
    part_key()
    part_encryption()
    part_subscriptions()
    part_dispatch()
    part_without_key()
    part_with_key()

    # What this script cannot do belongs in the output and not only in the
    # source - a passed run that conceals what it did not touch inspires more
    # trust than it deserves.
    return verify.balance(
        "Nicht geprüft (und nicht prüfbar ohne echtes Gerät): der Sprung zum\n"
        "Push-Dienst. Dafür gibt es POST /api/push/probe.")


if __name__ == "__main__":
    sys.exit(main())
