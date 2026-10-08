"""The scaffolding that all check scripts share.

Six scripts had the same `verify` function - in five of them byte-for-byte
identical -, the same failure list, the same evaluation at the end and the
same preamble that sets the search path and sets up a throwaway database.
That is not much code, but it is code that can drift apart six times, and
twice it already had.

Deliberately not an off-the-shelf test framework. What the scripts do is
not a collection of assertions but **one readable claim per line**:
"cold air is at least 7 % denser than warm". These sentences are the point
of the whole thing - they appear like that in the output, and whoever reads
them knows what jolt claims about itself. A framework that outputs
`test_air_density_cold PASSED` instead would not have this benefit.

Equally deliberately without dependencies: the scripts run without network,
without Postgres and without API key, and `check_model`, `check_optimizer`
and `check_sources` even run without the application installed. That stays
so.
"""
import os
import sys
import tempfile


class Check:
    """Collects results and knows at the end whether anything is missing.

    A class and not module variables, so that two scripts in the same
    process do not share the failure list - when combining several checks
    that would otherwise be a silent source of errors.
    """

    def __init__(self) -> None:
        self.failure: list[str] = []

    def __call__(self, condition, text: str, extra: str = "") -> None:
        """Check a claim and log it in one sentence.

        `extra` appears only in the failure case and is meant to carry the
        **measured value**, not a repetition of the claim: whoever sees that
        2.077 came out instead of 1.23 knows immediately what to look for.
        """
        if condition:
            print(f"  ok    {text}")
        else:
            print(f"  FEHLT {text}   {extra}")
            self.failure.append(text)

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def balance(self, suffix: str = "") -> int:
        """Return value for `sys.exit` - 0 if everything held.

        `suffix` is for whatever a script *cannot* check. This limitation
        belongs in the output and not only in the source: a passed run that
        conceals what it did not touch inspires more trust than it deserves.
        """
        print()
        if self.failure:
            print(f"{len(self.failure)} Prüfung(en) fehlgeschlagen:")
            for text in self.failure:
                print(f"  - {text}")
            return 1
        print("Alle Prüfungen bestanden.")
        if suffix:
            print(f"\n{suffix}")
        return 0


def application_provide(brand: str, *, db_name: bool = True) -> None:
    """Set the search path and, if necessary, set up a throwaway database.

    **Call before any app import.** `app.database` builds the engine at
    import time; whoever sets `DATABASE_URL` afterwards changes nothing any
    more and writes to the development database - in the worst case the real
    one.

    `ORS_API_KEY` and `APP_PASSWORT` are removed so that a run on a set-up
    machine does the same as on a bare one: demo routing, no login. A check
    script whose result depends on the environment checks the environment.
    """
    # Two layouts, and both must work. Locally the package lives under
    # `backend/app`; in the Docker image it sits directly next to `tools/` as
    # `app/`. The four import tools in this folder have always been able to do
    # that, `examine.py` could not - it was hard-wired to `../backend`.
    # As a result, `docker exec jolt-app python tools/check_*.py` ran **not a
    # single** check script; each one aborted with `ModuleNotFoundError:
    # No module named 'app'`. Of all things the very route for which `tools/`
    # was included in the image in the first place.
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "..", "backend"),
                     os.path.join(here, "..")):
        if os.path.isdir(os.path.join(candidate, "app")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            break

    if db_name:
        folder = tempfile.mkdtemp(prefix=f"jolt-{brand}-")
        os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(folder, 'check.db')}"
    os.environ.pop("ORS_API_KEY", None)
    os.environ.pop("APP_PASSWORT", None)
