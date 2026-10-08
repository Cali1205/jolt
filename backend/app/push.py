"""Web Push: eine Planänderung erreicht das Telefon auch mit dunklem Bildschirm.

Die Live-Ansicht meldet eine Änderung schon selbst - aber nur, solange sie offen
und der Bildschirm an ist. Wer das Telefon in die Tasche gesteckt hat, erfährt
sonst erst an der Säule, dass der Plan ein anderer ist. Genau dafür gibt es
Web Push: Der Server schickt die Nachricht an den Push-Dienst des Browsers
(Google, Mozilla, Apple), der sie an das Gerät zustellt.

**Ohne VAPID-Schlüssel ist die Funktion aus.** Das ist dieselbe Haltung wie bei
`ORS_API_KEY` und `APP_PASSWORT`: Was nicht eingerichtet ist, wird nicht
vorgetäuscht - es steht beim Start im Log und die Oberfläche sagt es dazu.
Schlüssel erzeugt `tools/push_keyname.py`.

Zwei Dinge, die hier bewusst so und nicht anders sind:

- **Der Versand ist einspeisbar** (`versender`). Die Verschlüsselung, die Wahl
  der Empfänger und das Aufräumen toter Abos lassen sich damit vollständig
  prüfen, ohne einen Push-Dienst zu erreichen - siehe tools/check_push.py. Nur
  der Netzsprung selbst bleibt ungetestet, und der gehört auch nicht in ein
  Prüfskript.
- **Ein totes Abo wird gelöscht, kein anderer Fehler.** Ein Browser, der die
  Erlaubnis entzogen hat, antwortet mit 404 oder 410; das Abo ist dann endgültig
  wertlos. Ein Zeitfehler oder eine 500 des Push-Dienstes sagt dagegen nichts
  über das Abo aus - wer es dabei wegwirft, schaltet die Benachrichtigungen bei
  der ersten Störung dauerhaft ab.
"""
import base64
import json
import logging
import os
import ipaddress
import socket
import threading
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import models

log = logging.getLogger("uvicorn.error")

# Wie lange auf den Push-Dienst gewartet wird. Die Nachricht ist unterwegs
# relevant oder gar nicht; ein Aufruf, der eine Minute hängt, hilft niemandem
# und hält einen Thread fest.
TIME_LIMIT_S = 10

# Antworten, nach denen ein Abo endgültig weg ist.
TOT = (404, 410)


# ---------------------------------------------------------------------------
# Schlüssel
# ---------------------------------------------------------------------------

def _b64(raw_data: bytes) -> str:
    return base64.urlsafe_b64encode(raw_data).rstrip(b"=").decode("ascii")


def generate_key() -> tuple[str, str]:
    """Ein neues VAPID-Schlüsselpaar: (privat, öffentlich), beide base64url.

    Der öffentliche Schlüssel ist der unkomprimierte Punkt (65 Byte) - genau
    das Format, das der Browser als `applicationServerKey` erwartet.
    """
    private_ = ec.generate_private_key(ec.SECP256R1())
    raw_private_ = private_.private_numbers().private_value.to_bytes(32, "big")
    raw_public_ = private_.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(raw_private_), _b64(raw_public_)


def private_key() -> str:
    return os.environ.get("VAPID_PRIVATE_KEY", "").strip()


def pub_key() -> str:
    return os.environ.get("VAPID_PUBLIC_KEY", "").strip()


def sender() -> str:
    """Die `sub`-Angabe der VAPID-Behauptung.

    Der Push-Dienst will wissen, wen er erreichen kann, wenn ein Server
    auffällig wird. Eine mailto:- oder https:-Adresse, sonst lehnen manche
    Dienste ab.
    """
    val = os.environ.get("VAPID_SUBJECT", "").strip()
    return val or "mailto:jolt@localhost"


def actual_configured() -> bool:
    return bool(private_key() and pub_key())


def at_start_warn() -> None:
    if not actual_configured():
        log.info("Kein VAPID-Schlüssel gesetzt - Benachrichtigungen aufs Telefon "
                 "sind aus. Schlüssel erzeugen: tools/push_keyname.py")


# ---------------------------------------------------------------------------
# Abos
# ---------------------------------------------------------------------------

def save_subscription(db, endpoint: str, p256dh: str, auth: str,
                  device: str = "") -> models.PushSubscription:
    """Ein Abo anlegen oder auffrischen.

    Idempotent über den Endpunkt: Der Browser liefert bei jedem Aufruf
    denselben, solange die Erlaubnis besteht. Ein zweiter Aufruf soll das Abo
    erneuern und nicht verdoppeln - sonst bekäme dasselbe Telefon die
    Benachrichtigung mehrfach.
    """
    subscription = db.query(models.PushSubscription).filter_by(endpoint=endpoint).one_or_none()
    if subscription is None:
        subscription = models.PushSubscription(endpoint=endpoint)
        db.add(subscription)
    subscription.p256dh = p256dh
    subscription.auth = auth
    subscription.device = (device or "")[:120]
    subscription.failure = 0
    db.commit()
    return subscription


def delete_subscription(db, endpoint: str) -> bool:
    subscription = db.query(models.PushSubscription).filter_by(endpoint=endpoint).one_or_none()
    if subscription is None:
        return False
    db.delete(subscription)
    db.commit()
    return True


def _as_subscription(subscription: models.PushSubscription) -> dict:
    """In die Form bringen, die pywebpush erwartet."""
    return {"endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth}}


# ---------------------------------------------------------------------------
# Versand
# ---------------------------------------------------------------------------

def endpoint_allowed(endpoint: str) -> bool:
    """Ob der Server diese Adresse als Push-Dienst anrufen darf.

    Der Endpunkt kommt vom Browser, also von jedem, der Zugang hat - und der
    Server ruft ihn später selbst an (SSRF). Deshalb: nur https, und der Name
    muss auf öffentliche Adressen zeigen. Loopback, private Netze, link-local
    (Cloud-Metadaten 169.254.169.254) und die Docker-Namen wie `db` fallen
    damit heraus. Nicht auflösbar heisst abgelehnt.
    """
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        if parts.scheme != "https" or not host or parts.username or parts.password:
            return False
        addresses = {a[4][0] for a in socket.getaddrinfo(host, parts.port or 443,
                                                        proto=socket.IPPROTO_TCP)}
    except (ValueError, OSError):
        return False
    if not addresses:
        return False
    return all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


def _send_real(subscription: models.PushSubscription, msg: bytes) -> int:
    """Der wirkliche Versand an den Push-Dienst. Gibt den HTTP-Status zurück."""
    from pywebpush import WebPushException, webpush

    # Auch beim Senden prüfen: Ein altes Abo oder ein Name, der inzwischen
    # woanders hinzeigt, darf den Server nicht ins eigene Netz schicken.
    if not endpoint_allowed(subscription.endpoint):
        return 0
    try:
        response = webpush(
            subscription_info=_as_subscription(subscription), data=msg,
            vapid_private_key=private_key(),
            vapid_claims={"sub": sender()},
            content_encoding="aes128gcm", timeout=TIME_LIMIT_S)
        return getattr(response, "status_code", 201)
    except WebPushException as failure:
        response = getattr(failure, "response", None)
        # Ohne Antwort ist es ein Netzproblem, kein Urteil über das Abo.
        return getattr(response, "status_code", 0) if response is not None else 0


def send(db, title: str, text: str, url: str = "/", dispatcher=None) -> dict:
    """Eine Benachrichtigung an alle Abos. Räumt tote Abos dabei auf.

    `versender(abo, nachricht) -> HTTP-Status` lässt sich ersetzen; damit sind
    Auswahl, Nutzlast und Aufräumen prüfbar, ohne einen Push-Dienst zu
    erreichen.
    """
    if dispatcher is None:
        if not actual_configured():
            return {"sent": 0, "removed": 0, "failure": 0, "origin_of": True}
        dispatcher = _send_real

    msg = json.dumps({"title": title, "text": text, "url": url},
                           ensure_ascii=False).encode("utf-8")

    sent = failure = 0
    tot: list[models.PushSubscription] = []
    for subscription in db.query(models.PushSubscription).all():
        try:
            status = dispatcher(subscription, msg)
        except Exception as exception:      # noqa: BLE001
            log.warning("Push an %s fehlgeschlagen: %s", subscription.endpoint[:60],
                        exception)
            status = 0

        if status in TOT:
            tot.append(subscription)
        elif 200 <= status < 300:
            sent += 1
            subscription.failure = 0
        else:
            failure += 1
            subscription.failure = (subscription.failure or 0) + 1

    for subscription in tot:
        log.info("Push-Abo entfernt (abgemeldet): %s", subscription.endpoint[:60])
        db.delete(subscription)
    db.commit()

    return {"sent": sent, "removed": len(tot), "failure": failure,
            "origin_of": False}


def send_background(db_factory, title: str, text: str, url: str = "/") -> None:
    """Wie `senden`, aber ohne den Aufrufer aufzuhalten.

    Ein Messpunkt kommt aus einem fahrenden Auto; die Antwort darauf darf nicht
    auf einen Push-Dienst warten. Der Thread bekommt eine eigene
    Datenbanksitzung - eine über Threads geteilte wäre genau der Fehler, den
    SQLAlchemy nicht verzeiht.
    """
    if not actual_configured():
        return

    def cycle():
        db = db_factory()
        try:
            send(db, title, text, url)
        except Exception as failure:      # noqa: BLE001
            log.warning("Push im Hintergrund fehlgeschlagen: %s", failure)
        finally:
            db.close()

    threading.Thread(target=cycle, daemon=True, name="jolt-push").start()
