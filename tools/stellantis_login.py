#!/usr/bin/env python3
"""Log jolt in to the Stellantis connected-car API once (Opel, Peugeot,
Citroën, DS) and store the tokens in the database.

The brand's login page shows a captcha and ends on the brand app's own
address (mymopsdk://... for Opel), which no browser opens. So the login runs
in your browser, and you copy the `code` from that last address back here.
The same flow as evcc's `evcc token` (cmd/token_psa.go, MIT license).

    docker compose run --rm --no-deps -it jolt-app python tools/stellantis_login.py

Brand, country and account come from STELLANTIS_BRAND, STELLANTIS_COUNTRY
and STELLANTIS_USER in .env, or from --brand, --country and --user. Restart
jolt-app afterwards so it starts fetching (docs/stellantis.md).
"""
import argparse
import os
import sys
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (os.path.join(HERE, "..", "backend"), os.path.join(HERE, "..")):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break

from app.cloud import credentials, mask, stellantis  # noqa: E402
from app.database import SessionLocal, migrate  # noqa: E402

INSTRUCTIONS = """
1. Öffne im Browser die Entwicklerwerkzeuge (F12), Reiter "Netzwerk"
   (Network), und dann diese Adresse. Melde dich mit deinem {Brand}-Konto an.
   Open the developer tools (F12), tab "Network", then this address, and log
   in with your {Brand} account:

   {url}

2. Am Ende will der Browser eine Adresse {scheme}://oauth2redirect/... öffnen
   und kann es nicht: eine Fehlerseite, oder es passiert scheinbar nichts. Das
   ist richtig so. In der Adresszeile oder im Reiter "Netzwerk" (letzte
   Anfrage, Kopfzeile "Location") steht diese Adresse mit "code=...".
   At the end the browser tries to open {scheme}://oauth2redirect/... and
   cannot. That is expected. The address bar or the network tab (last request,
   "Location" header) shows that address with "code=...".

3. Kopiere den Code oder die ganze Adresse hierher. Er gilt nur wenige
   Minuten. Copy the code or the whole address here; it is valid for a few
   minutes only.
"""


def code_from(text: str) -> str:
    """The authorization code from a pasted redirect address or the bare code."""
    text = text.strip().strip('"').strip("'")
    if "code=" in text:
        query = urlsplit(text).query or text.split("?", 1)[-1]
        codes = parse_qs(query).get("code")
        if codes:
            return codes[0].strip()
    return text


def main(argv=None, read=input, adapter=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand", default=os.environ.get("STELLANTIS_BRAND", "").strip() or "opel",
                        choices=sorted(stellantis.BRANDS))
    parser.add_argument("--country", default=os.environ.get("STELLANTIS_COUNTRY", "").strip() or "de")
    parser.add_argument("--user", default=os.environ.get("STELLANTIS_USER", "").strip(),
                        help="the account's e-mail address (as in STELLANTIS_USER)")
    args = parser.parse_args(argv)
    if not args.user:
        print("STELLANTIS_USER fehlt (missing): --user name@example.org oder in .env.")
        return 2
    brand = stellantis.BRANDS[args.brand]
    verifier, challenge = stellantis.pkce_pair()
    print(INSTRUCTIONS.format(Brand=args.brand.capitalize(), scheme=brand.scheme,
                              url=stellantis.authorize_url(args.brand, args.country, challenge)))
    code = code_from(read("Code: "))
    if not code:
        print("Kein Code eingegeben (no code). Abbruch.")
        return 1
    try:
        tokens = stellantis.exchange_code(args.brand, args.country, code, verifier,
                                          adapter=adapter)
    except stellantis.StellantisError as failure:
        print(f"Anmeldung fehlgeschlagen (login failed): {failure}")
        print("Ein Code gilt nur einmal und wenige Minuten - nochmal von vorn.")
        return 1
    migrate()
    db = SessionLocal()
    try:
        credentials.save(db, stellantis.SOURCE,
                         stellantis.account_key(args.brand, args.user), tokens)
    finally:
        db.close()
    print("Gespeichert (stored).")
    try:
        vins = stellantis.Stellantis(args.brand, args.user, SessionLocal,
                                     adapter=adapter).vehicles()
        print("Fahrzeuge (vehicles): " + (", ".join(mask(v) for v in vins) or "keine (none)"))
    except stellantis.StellantisError as failure:
        print(f"Fahrzeugliste nicht abrufbar (vehicle list failed): {failure}")
    print("\nJetzt jolt-app neu starten, nicht während einer Aufzeichnung (restart, "
          "not during a recording):\n  docker compose up -d --force-recreate jolt-app")
    return 0


if __name__ == "__main__":
    sys.exit(main())
