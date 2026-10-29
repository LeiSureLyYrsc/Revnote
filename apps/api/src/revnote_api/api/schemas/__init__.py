"""Wire schemas of the ``/api/v1`` contract.

One module per endpoint group (``auth``, ``sync_push``, ``sync_pull``, ``read``)
plus :mod:`revnote_api.api.schemas.common` for the pieces every group shares:
the decimal-string counter type, the RFC 3339 UTC timestamp type and the fixed
error envelope. Only ``common`` exists in this commit — the endpoint schemas are
added together with the endpoints that use them, so the published OpenAPI
document never advertises a route that cannot be served.
"""

from __future__ import annotations
