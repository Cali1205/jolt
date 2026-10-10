"""Which version is running, and which commit it was built from.

The number comes from the `VERSION` file in the repository root (in the image:
next to `app/`), the build from the build argument `GIT_SHA` that the CI sets
when it builds the image. A deploy is thus provable from the outside: the
number stays until someone raises it, the build changes with every merge.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _read_version() -> str:
    for base in (os.path.join(_HERE, ".."), os.path.join(_HERE, "..", "..")):
        try:
            with open(os.path.join(base, "VERSION"), encoding="utf-8") as file:
                return file.read().strip() or "?"
        except OSError:
            continue
    return "?"


VERSION = _read_version()
BUILD = (os.environ.get("GIT_SHA", "") or "dev")[:7]
