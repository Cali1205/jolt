#!/usr/bin/env python3
"""Prüft die Benachrichtigungen aufs Telefon - Web Push.

Was hier **nicht** geprüft wird, und warum: der Netzsprung zum Push-Dienst
(Google, Mozilla, Apple). Der gehört nicht in ein Prüfskript - er braucht ein
echtes Gerät mit einem echten Abo, und ein Skript, das gegen fremde Dienste
läuft, schlägt irgendwann aus Gründen fehl, die nichts mit jolt zu tun haben.

Was geprüft wird, ist alles davor - und das ist das meiste:

- **Die Schlüssel.** Erzeugt jolt ein Paar, das py_vapid annimmt, und passt der
  öffentliche zum privaten? Ein Schlüssel im falschen Format fällt sonst erst
  am Telefon auf.
- **Die Verschlüsselung, im Rundlauf.** Die Nutzlast wird für ein
  nachgebautes Browser-Abo verschlüsselt und mit dessen privatem Schlüssel
  wieder entschlüsselt. Kommt der Klartext zurück, stimmt der ganze Pfad nach
  RFC 8291 - das ist der Teil, an dem Web Push in der Praxis scheitert.
- **Die Abo-Verwaltung.** Anlegen ist idempotent, Abmelden wirkt.
- **Das Aufräumen.** Ein Abo, dessen Browser sich abgemeldet hat (404/410),
  verschwindet. Ein Zeitfehler oder eine 500 dagegen nicht - wer Abos bei der
  ersten Störung wegwirft, schaltet die Benachrichtigungen dauerhaft ab.
- **Der Auslöser.** Ohne Schlüssel ist die Funktion aus und behauptet nichts.

    ./tools/check_push.py
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

anwendung_bereitstellen("push")

import http_ece  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from py_vapid import Vapid01  # noqa: E402
from pywebpush import WebPusher  # noqa: E402

from app import models, push  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

pruefe = Pruefung()


def b64(rohdaten: bytes) -> str:
    return base64.urlsafe_b64encode(rohdaten).rstrip(b"=").decode("ascii")


class Browserabo:
    """Ein nachgebautes Abo, wie es ein Browser anlegen würde.

    Es hat einen echten privaten Schlüssel - nur damit lässt sich prüfen, ob
    die verschlüsselte Nutzlast beim Empfänger wieder lesbar wird.
    """

    def __init__(self, endpoint: str = "https://push.example.org/abo-1"):
        self.endpoint = endpoint
        self.privat = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.p256dh = self.privat.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)

    def als_json(self) -> dict:
        return {"endpoint": self.endpoint, "p256dh": b64(self.p256dh),
                "auth": b64(self.auth), "geraet": "Prüf-Browser"}

    def entschluesseln(self, koerper: bytes) -> dict:
        klar = http_ece.decrypt(koerper, private_key=self.privat,
                                auth_secret=self.auth, version="aes128gcm")
        return json.loads(klar.decode("utf-8"))


# ---------------------------------------------------------------------------

def teil_schluessel():
    print("\nVAPID-Schlüssel")
    privat, oeffentlich = push.schluessel_erzeugen()
    roh_oeffentlich = base64.urlsafe_b64decode(
        oeffentlich + "=" * (-len(oeffentlich) % 4))
    pruefe(len(roh_oeffentlich) == 65,
           "der öffentliche Schlüssel ist ein unkomprimierter Punkt (65 Byte) - "
           "genau das erwartet der Browser", f"{len(roh_oeffentlich)} Byte")
    pruefe(roh_oeffentlich[0] == 0x04,
           "und beginnt mit 0x04, wie es die Kodierung verlangt")

    # Nimmt py_vapid den privaten Schlüssel an, und gehören beide zusammen?
    vapid = Vapid01.from_string(privat)
    abgeleitet = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    pruefe(b64(abgeleitet) == oeffentlich,
           "der öffentliche Schlüssel gehört zum privaten - sonst nimmt kein "
           "Push-Dienst die Nachricht an")

    kopfzeilen = vapid.sign({"aud": "https://push.example.org",
                             "sub": "mailto:jolt@example.org"})
    pruefe("Authorization" in kopfzeilen
           and kopfzeilen["Authorization"].startswith("WebPush "),
           "aus dem Schlüssel entsteht eine gültige Authorization-Kopfzeile",
           str(sorted(kopfzeilen))[:80])


def teil_verschluesselung():
    print("\nNutzlast verschlüsseln und wieder lesen (RFC 8291)")
    browser = Browserabo()
    inhalt = json.dumps({"titel": "jolt – Ladeplan geändert",
                         "text": "Nächster Stopp jetzt Rasthof Nord bei km 212.",
                         "url": "/"}, ensure_ascii=False).encode("utf-8")

    verschluesselt = WebPusher({"endpoint": browser.endpoint,
                                "keys": {"p256dh": b64(browser.p256dh),
                                         "auth": b64(browser.auth)}}
                               ).encode(inhalt, content_encoding="aes128gcm")
    koerper = verschluesselt["body"] if isinstance(verschluesselt, dict) \
        else verschluesselt
    pruefe(len(koerper) > len(inhalt),
           "die verschlüsselte Nachricht ist länger als der Klartext",
           f"{len(koerper)} statt {len(inhalt)} Byte")
    pruefe(inhalt not in koerper,
           "und der Klartext steht nicht mehr darin - der Push-Dienst kann "
           "nicht mitlesen")

    zurueck = browser.entschluesseln(koerper)
    pruefe(zurueck["text"].startswith("Nächster Stopp"),
           "der Empfänger bekommt den Klartext zurück", str(zurueck)[:70])
    pruefe("Ladeplan geändert" in zurueck["titel"],
           "auch Umlaute überstehen den Rundlauf", zurueck["titel"])


def teil_abos():
    print("\nAbos anlegen, auffrischen, abmelden")
    db = SessionLocal()
    try:
        db.query(models.PushAbo).delete()
        db.commit()

        browser = Browserabo()
        daten = browser.als_json()
        push.abo_speichern(db, daten["endpoint"], daten["p256dh"],
                           daten["auth"], daten["geraet"])
        pruefe(db.query(models.PushAbo).count() == 1, "ein Abo ist angelegt")

        push.abo_speichern(db, daten["endpoint"], daten["p256dh"],
                           daten["auth"], "Anderes Gerät")
        anzahl = db.query(models.PushAbo).count()
        pruefe(anzahl == 1,
               "ein zweiter Aufruf legt nichts doppelt an - sonst käme jede "
               "Meldung mehrfach", f"{anzahl} Abos")
        gespeichert = db.query(models.PushAbo).one()
        pruefe(gespeichert.geraet == "Anderes Gerät",
               "aber er frischt das Abo auf", gespeichert.geraet)

        pruefe(push.abo_loeschen(db, daten["endpoint"]) is True,
               "das Abo lässt sich abmelden")
        pruefe(db.query(models.PushAbo).count() == 0, "und ist dann weg")
        pruefe(push.abo_loeschen(db, "gibt-es-nicht") is False,
               "ein unbekanntes Abo abzumelden ist kein Fehler")
    finally:
        db.close()


def teil_versand():
    print("\nVersand: wer bekommt was, und was passiert bei Fehlern")
    db = SessionLocal()
    try:
        db.query(models.PushAbo).delete()
        db.commit()

        lebt = Browserabo("https://push.example.org/lebt")
        abgemeldet = Browserabo("https://push.example.org/abgemeldet")
        gestoert = Browserabo("https://push.example.org/gestoert")
        for browser in (lebt, abgemeldet, gestoert):
            d = browser.als_json()
            push.abo_speichern(db, d["endpoint"], d["p256dh"], d["auth"])

        empfangen: dict[str, bytes] = {}

        def versender(abo, nachricht):
            empfangen[abo.endpoint] = nachricht
            if abo.endpoint.endswith("abgemeldet"):
                return 410      # der Browser hat die Erlaubnis entzogen
            if abo.endpoint.endswith("gestoert"):
                return 500      # der Push-Dienst hat gerade ein Problem
            return 201

        ergebnis = push.senden(db, "jolt", "Ladeplan geändert", versender=versender)
        pruefe(ergebnis["gesendet"] == 1, "ein Gerät hat die Meldung bekommen",
               str(ergebnis))
        pruefe(ergebnis["entfernt"] == 1, "ein abgemeldetes Abo wurde entfernt",
               str(ergebnis))
        pruefe(ergebnis["fehler"] == 1, "eine Störung wurde als Fehler gezählt",
               str(ergebnis))

        uebrig = {a.endpoint for a in db.query(models.PushAbo).all()}
        pruefe(abgemeldet.endpoint not in uebrig,
               "das abgemeldete Gerät steht nicht mehr in der Datenbank")
        pruefe(gestoert.endpoint in uebrig,
               "das gestörte dagegen schon - eine 500 sagt nichts über das Abo, "
               "und wer es wegwirft, schaltet Benachrichtigungen dauerhaft ab")

        inhalt = json.loads(empfangen[lebt.endpoint].decode("utf-8"))
        pruefe(inhalt["titel"] == "jolt" and inhalt["text"] == "Ladeplan geändert",
               "die Nutzlast trägt Titel und Text", str(inhalt))
        pruefe("url" in inhalt,
               "und ein Ziel für den Klick auf die Meldung")

        # Ein Versender, der wirft, darf den Versand an die anderen nicht
        # abbrechen - im Funkloch ist das der Normalfall.
        def wirft(abo, nachricht):
            if abo.endpoint.endswith("lebt"):
                raise OSError("Netz weg")
            return 201

        ergebnis = push.senden(db, "jolt", "zweiter Versuch", versender=wirft)
        pruefe(ergebnis["fehler"] == 1 and ergebnis["gesendet"] == 1,
               "ein geworfener Fehler bricht den Versand an die anderen nicht ab",
               str(ergebnis))
    finally:
        db.close()


def teil_ohne_schluessel():
    print("\nOhne VAPID-Schlüssel")
    alt = (os.environ.pop("VAPID_PRIVATE_KEY", None),
           os.environ.pop("VAPID_PUBLIC_KEY", None))
    try:
        pruefe(push.ist_eingerichtet() is False,
               "die Funktion meldet sich als nicht eingerichtet")

        db = SessionLocal()
        try:
            ergebnis = push.senden(db, "jolt", "sollte nicht rausgehen")
        finally:
            db.close()
        pruefe(ergebnis.get("aus") is True and ergebnis["gesendet"] == 0,
               "und es wird nichts verschickt - statt es vorzutäuschen",
               str(ergebnis))

        client = TestClient(app)
        antwort = client.get("/api/push/schluessel").json()
        pruefe(antwort["eingerichtet"] is False,
               "die Oberfläche erfährt das über /api/push/schluessel",
               str(antwort))

        angelegt = client.post("/api/push/abo", json={
            "endpoint": "https://push.example.org/x", "p256dh": "a" * 20,
            "auth": "b" * 10})
        pruefe(angelegt.status_code == 409,
               "ein Abo anzulegen wird sauber abgelehnt, nicht still verschluckt",
               f"HTTP {angelegt.status_code}")
    finally:
        for name, wert in zip(("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY"), alt):
            if wert is not None:
                os.environ[name] = wert


def teil_mit_schluessel():
    print("\nMit VAPID-Schlüssel über die Endpunkte")
    privat, oeffentlich = push.schluessel_erzeugen()
    os.environ["VAPID_PRIVATE_KEY"] = privat
    os.environ["VAPID_PUBLIC_KEY"] = oeffentlich
    os.environ["VAPID_SUBJECT"] = "mailto:jolt@example.org"
    try:
        client = TestClient(app)
        antwort = client.get("/api/push/schluessel").json()
        pruefe(antwort["eingerichtet"] is True, "eingerichtet")
        pruefe(antwort["schluessel"] == oeffentlich,
               "und der öffentliche Schlüssel kommt heraus")

        browser = Browserabo("https://push.example.org/ueber-api")
        angelegt = client.post("/api/push/abo", json=browser.als_json())
        pruefe(angelegt.status_code == 200, "ein Abo lässt sich anlegen",
               f"HTTP {angelegt.status_code}: {angelegt.text[:100]}")

        db = SessionLocal()
        try:
            gefunden = db.query(models.PushAbo).filter_by(
                endpoint=browser.endpoint).one_or_none()
        finally:
            db.close()
        pruefe(gefunden is not None, "und steht in der Datenbank")

        ab = client.request("DELETE", "/api/push/abo",
                            json={"endpoint": browser.endpoint})
        pruefe(ab.status_code == 200 and ab.json()["ok"] is True,
               "und wieder abmelden", f"HTTP {ab.status_code}")
    finally:
        for name in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_SUBJECT"):
            os.environ.pop(name, None)


def main() -> int:
    teil_schluessel()
    teil_verschluesselung()
    teil_abos()
    teil_versand()
    teil_ohne_schluessel()
    teil_mit_schluessel()

    # Was dieses Skript nicht kann, gehört in die Ausgabe und nicht nur in
    # den Quelltext - ein bestandener Lauf, der verschweigt, was er nicht
    # angefasst hat, weckt mehr Vertrauen als er verdient.
    return pruefe.bilanz(
        "Nicht geprüft (und nicht prüfbar ohne echtes Gerät): der Sprung zum\n"
        "Push-Dienst. Dafür gibt es POST /api/push/probe.")


if __name__ == "__main__":
    sys.exit(main())
