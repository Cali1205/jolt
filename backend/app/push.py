"""Web Push: eine Planänderung erreicht das Telefon auch mit dunklem Bildschirm.

Die Live-Ansicht meldet eine Änderung schon selbst - aber nur, solange sie offen
und der Bildschirm an ist. Wer das Telefon in die Tasche gesteckt hat, erfährt
sonst erst an der Säule, dass der Plan ein anderer ist. Genau dafür gibt es
Web Push: Der Server schickt die Nachricht an den Push-Dienst des Browsers
(Google, Mozilla, Apple), der sie an das Gerät zustellt.

**Ohne VAPID-Schlüssel ist die Funktion aus.** Das ist dieselbe Haltung wie bei
`ORS_API_KEY` und `APP_PASSWORT`: Was nicht eingerichtet ist, wird nicht
vorgetäuscht - es steht beim Start im Log und die Oberfläche sagt es dazu.
Schlüssel erzeugt `tools/push_schluessel.py`.

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
import threading

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import models

log = logging.getLogger("uvicorn.error")

# Wie lange auf den Push-Dienst gewartet wird. Die Nachricht ist unterwegs
# relevant oder gar nicht; ein Aufruf, der eine Minute hängt, hilft niemandem
# und hält einen Thread fest.
ZEITGRENZE_S = 10

# Antworten, nach denen ein Abo endgültig weg ist.
TOT = (404, 410)


# ---------------------------------------------------------------------------
# Schlüssel
# ---------------------------------------------------------------------------

def _b64(rohdaten: bytes) -> str:
    return base64.urlsafe_b64encode(rohdaten).rstrip(b"=").decode("ascii")


def schluessel_erzeugen() -> tuple[str, str]:
    """Ein neues VAPID-Schlüsselpaar: (privat, öffentlich), beide base64url.

    Der öffentliche Schlüssel ist der unkomprimierte Punkt (65 Byte) - genau
    das Format, das der Browser als `applicationServerKey` erwartet.
    """
    privat = ec.generate_private_key(ec.SECP256R1())
    roh_privat = privat.private_numbers().private_value.to_bytes(32, "big")
    roh_oeffentlich = privat.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(roh_privat), _b64(roh_oeffentlich)


def privater_schluessel() -> str:
    return os.environ.get("VAPID_PRIVATE_KEY", "").strip()


def oeffentlicher_schluessel() -> str:
    return os.environ.get("VAPID_PUBLIC_KEY", "").strip()


def absender() -> str:
    """Die `sub`-Angabe der VAPID-Behauptung.

    Der Push-Dienst will wissen, wen er erreichen kann, wenn ein Server
    auffällig wird. Eine mailto:- oder https:-Adresse, sonst lehnen manche
    Dienste ab.
    """
    wert = os.environ.get("VAPID_SUBJECT", "").strip()
    return wert or "mailto:jolt@localhost"


def ist_eingerichtet() -> bool:
    return bool(privater_schluessel() and oeffentlicher_schluessel())


def beim_start_warnen() -> None:
    if not ist_eingerichtet():
        log.info("Kein VAPID-Schlüssel gesetzt - Benachrichtigungen aufs Telefon "
                 "sind aus. Schlüssel erzeugen: tools/push_schluessel.py")


# ---------------------------------------------------------------------------
# Abos
# ---------------------------------------------------------------------------

def abo_speichern(db, endpoint: str, p256dh: str, auth: str,
                  geraet: str = "") -> models.PushAbo:
    """Ein Abo anlegen oder auffrischen.

    Idempotent über den Endpunkt: Der Browser liefert bei jedem Aufruf
    denselben, solange die Erlaubnis besteht. Ein zweiter Aufruf soll das Abo
    erneuern und nicht verdoppeln - sonst bekäme dasselbe Telefon die
    Benachrichtigung mehrfach.
    """
    abo = db.query(models.PushAbo).filter_by(endpoint=endpoint).one_or_none()
    if abo is None:
        abo = models.PushAbo(endpoint=endpoint)
        db.add(abo)
    abo.p256dh = p256dh
    abo.auth = auth
    abo.geraet = (geraet or "")[:120]
    abo.fehler = 0
    db.commit()
    return abo


def abo_loeschen(db, endpoint: str) -> bool:
    abo = db.query(models.PushAbo).filter_by(endpoint=endpoint).one_or_none()
    if abo is None:
        return False
    db.delete(abo)
    db.commit()
    return True


def _als_abo(abo: models.PushAbo) -> dict:
    """In die Form bringen, die pywebpush erwartet."""
    return {"endpoint": abo.endpoint,
            "keys": {"p256dh": abo.p256dh, "auth": abo.auth}}


# ---------------------------------------------------------------------------
# Versand
# ---------------------------------------------------------------------------

def _echt_senden(abo: models.PushAbo, nachricht: bytes) -> int:
    """Der wirkliche Versand an den Push-Dienst. Gibt den HTTP-Status zurück."""
    from pywebpush import WebPushException, webpush

    try:
        antwort = webpush(
            subscription_info=_als_abo(abo), data=nachricht,
            vapid_private_key=privater_schluessel(),
            vapid_claims={"sub": absender()},
            content_encoding="aes128gcm", timeout=ZEITGRENZE_S)
        return getattr(antwort, "status_code", 201)
    except WebPushException as fehler:
        antwort = getattr(fehler, "response", None)
        # Ohne Antwort ist es ein Netzproblem, kein Urteil über das Abo.
        return getattr(antwort, "status_code", 0) if antwort is not None else 0


def senden(db, titel: str, text: str, url: str = "/", versender=None) -> dict:
    """Eine Benachrichtigung an alle Abos. Räumt tote Abos dabei auf.

    `versender(abo, nachricht) -> HTTP-Status` lässt sich ersetzen; damit sind
    Auswahl, Nutzlast und Aufräumen prüfbar, ohne einen Push-Dienst zu
    erreichen.
    """
    if versender is None:
        if not ist_eingerichtet():
            return {"gesendet": 0, "entfernt": 0, "fehler": 0, "aus": True}
        versender = _echt_senden

    nachricht = json.dumps({"titel": titel, "text": text, "url": url},
                           ensure_ascii=False).encode("utf-8")

    gesendet = fehler = 0
    tot: list[models.PushAbo] = []
    for abo in db.query(models.PushAbo).all():
        try:
            status = versender(abo, nachricht)
        except Exception as ausnahme:      # noqa: BLE001
            log.warning("Push an %s fehlgeschlagen: %s", abo.endpoint[:60],
                        ausnahme)
            status = 0

        if status in TOT:
            tot.append(abo)
        elif 200 <= status < 300:
            gesendet += 1
            abo.fehler = 0
        else:
            fehler += 1
            abo.fehler = (abo.fehler or 0) + 1

    for abo in tot:
        log.info("Push-Abo entfernt (abgemeldet): %s", abo.endpoint[:60])
        db.delete(abo)
    db.commit()

    return {"gesendet": gesendet, "entfernt": len(tot), "fehler": fehler,
            "aus": False}


def senden_hintergrund(db_factory, titel: str, text: str, url: str = "/") -> None:
    """Wie `senden`, aber ohne den Aufrufer aufzuhalten.

    Ein Messpunkt kommt aus einem fahrenden Auto; die Antwort darauf darf nicht
    auf einen Push-Dienst warten. Der Thread bekommt eine eigene
    Datenbanksitzung - eine über Threads geteilte wäre genau der Fehler, den
    SQLAlchemy nicht verzeiht.
    """
    if not ist_eingerichtet():
        return

    def lauf():
        db = db_factory()
        try:
            senden(db, titel, text, url)
        except Exception as fehler:      # noqa: BLE001
            log.warning("Push im Hintergrund fehlgeschlagen: %s", fehler)
        finally:
            db.close()

    threading.Thread(target=lauf, daemon=True, name="jolt-push").start()
