"""Single source of the runtime version string.

Kept in its own module (rather than ``__init__``) so submodules can import it
without triggering a circular import through the package ``__init__``.
``tests/unit/test_app.py`` asserts this stays in sync with the installed
distribution metadata (``pyproject.toml``).
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Distribution name (see apps/api/pyproject.toml).
DIST_NAME = "revnote-api"

__all__ = ["DIST_NAME", "__version__"]
