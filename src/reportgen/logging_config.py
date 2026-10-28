"""Logging setup. Secrets are never logged: callers must pass already-masked
connection strings (see ``reportgen.utils.security.mask_url``).

Handlers are (re)created on every ``configure_logging`` call so the
handler always writes to the *current* ``sys.stderr``. Caching a stream
object would break when reportgen is invoked repeatedly inside one
process (e.g. under a test runner or as a library), where stderr is
swapped between calls.
"""

from __future__ import annotations

import logging


def configure_logging(verbose: bool = False, quiet: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    root = logging.getLogger("reportgen")
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler()  # binds the current sys.stderr
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.propagate = False
    return root


def get_logger(name: str = "reportgen") -> logging.Logger:
    return logging.getLogger(name)
