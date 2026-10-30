"""``revnote_api.tools`` — operator entry points that are not part of the API.

Each module here is a ``python -m`` target with its own CLI. Nothing in this
package is imported by the application at runtime, so a tool can be as strict or as
chatty as an operator needs without becoming a request-path dependency.
"""

from __future__ import annotations

__all__: list[str] = []
