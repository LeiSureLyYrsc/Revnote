"""Native ``/api/v1`` HTTP client used by every command.

There is exactly one HTTP seam in the CLI; commands never call ``httpx`` directly.
That keeps three properties in one place:

* **Bearer handling.** The access token is attached here, a ``401`` triggers one
  refresh-and-retry, and a refresh that the server refuses clears the stored
  session instead of looping. Tokens are never formatted into a message.
* **Pagination.** ``snapshot`` consumes the Phase 7 ``limit``/``cursor`` protocol
  until ``has_more`` is false, pinning the first page's ``as_of_seq`` and refusing
  any later page that reports a different watermark. ``pull`` chains
  ``next_cursor`` the same way. Both are bounded (``MAX_PAGES``) so a broken
  server cannot trap the CLI in an endless loop.
* **Error mapping.** An HTTP error becomes :class:`CliError` carrying the
  server's stable code, so the process exit code is derived from the protocol
  rather than from a status number guessed at the call site.

Response validation is deliberately literal: counters are canonical decimal
strings, ``has_more`` is a boolean, and a page that contradicts its own cursor is
``invalid_response`` (exit 5) rather than silently concatenated. The Web client
makes the same checks; both consumers must see the same protocol.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from revnote_cli import __version__
from revnote_cli.config import CredentialStore, StoredCredentials
from revnote_cli.errors import EXIT_API, EXIT_AUTH, EXIT_USAGE, CliError

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGES",
    "RevnoteClient",
    "guess_mime",
]

#: Page size requested when the capability advertises a larger server maximum.
DEFAULT_PAGE_SIZE = 500

#: Hard stop for a paged bootstrap or a drained pull.
MAX_PAGES = 1_000

#: Canonical unsigned decimal (no sign, no leading zero, 19 digits max).
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")

#: Statuses a push result may carry.
_PUSH_STATUSES = frozenset(
    {"applied", "replayed", "conflict", "rejected", "unknown", "not_attempted"}
)


def _invalid(what: str) -> CliError:
    """A response that violates the published protocol."""
    return CliError(
        f"The server returned an invalid {what}.",
        code="invalid_response",
        exit_code=EXIT_API,
    )


def _require_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(what)
    return value


def _require_list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise _invalid(what)
    return value


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(what)
    return value


def _require_decimal(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise _invalid(what)
    return value


def _require_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(what)
    return value


class RevnoteClient:
    """One synchronous client bound to one API base and one credential file."""

    def __init__(
        self,
        credentials: StoredCredentials,
        store: CredentialStore,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.credentials = credentials
        self.store = store
        self.api_base = (credentials.api_base or "").rstrip("/")
        if not self.api_base:
            raise CliError("No API base URL is configured.", code="usage", exit_code=EXIT_USAGE)
        self.http = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Accept": "application/json",
                "User-Agent": f"revnote-cli/{__version__}",
            },
            follow_redirects=False,
        )

    # -- lifecycle ----------------------------------------------------------- #
    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> RevnoteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- authentication ------------------------------------------------------ #
    def login(
        self, email: str, password: str, *, device_name: str = "revnote-cli"
    ) -> dict[str, Any]:
        """Exchange credentials for a token pair and persist it."""
        response = self._send(
            "POST",
            "/api/v1/auth/login",
            json_body={"email": email, "password": password, "device_name": device_name},
            authenticate=False,
        )
        payload = self._json(response)
        self._adopt_token_pair(payload)
        return payload

    def refresh(self) -> dict[str, Any]:
        """Rotate the refresh token explicitly (``revnote refresh``)."""
        return self._refresh_tokens()

    def logout(self) -> bool:
        """Revoke the current session and delete the local file.

        A session the server already considers gone is still a successful logout.
        A transport failure clears the local token too - "log out" must not leave a
        credential on disk because the network was down - and then reports exit 5
        so a script can tell the revocation did not complete.
        """
        if not self.credentials.access_token:
            self.store.clear()
            return False
        failure: CliError | None = None
        try:
            self._send("POST", "/api/v1/auth/logout", authenticate=True, allow_refresh=False)
        except CliError as error:
            if error.exit_code == EXIT_API:
                failure = error
        self._forget_tokens()
        self.store.clear()
        if failure is not None:
            raise failure
        return True

    def me(self) -> dict[str, Any]:
        """The authenticated principal and its workspace memberships."""
        return self._json(self._send("GET", "/api/v1/me"))

    def capabilities(self) -> dict[str, Any]:
        """Deployment feature detection; unauthenticated by contract."""
        return self._json(self._send("GET", "/api/v1/capabilities", authenticate=False))

    # -- reads --------------------------------------------------------------- #
    def tree(self, workspace_id: str) -> dict[str, Any]:
        return self._json(self._send("GET", f"/api/v1/workspaces/{workspace_id}/tree"))

    def note(self, workspace_id: str, note_id: str) -> dict[str, Any]:
        return self._json(self._send("GET", f"/api/v1/workspaces/{workspace_id}/notes/{note_id}"))

    # -- sync ---------------------------------------------------------------- #
    def snapshot(self, workspace_id: str, *, page_size: int | None = None) -> dict[str, Any]:
        """Bootstrap the active object set, consuming Phase 7 pages when offered.

        ``page_size`` is an explicit client request: supplying it selects the paged
        path even when the capability probe is absent (an older server answers
        ``invalid_request``/``404`` and that is reported honestly). Without an
        explicit size the capability decides: paged loop when enabled, legacy
        all-or-nothing read otherwise.
        """
        try:
            capability = self.capabilities().get("snapshot_pagination")
        except CliError as error:
            # A server that predates the capability endpoint (or deliberately
            # disables it) has no paging to advertise; the legacy read is the
            # honest fallback, exactly as the Web transport treats a 404.
            if error.status == 404 or error.code == "not_found":
                capability = None
            else:
                raise
        server_max: int | None = None
        enabled = False
        if isinstance(capability, dict):
            enabled = capability.get("enabled") is True
            raw_max = capability.get("page_max_objects")
            if isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max >= 1:
                server_max = raw_max

        if not enabled and page_size is None:
            return self._snapshot_legacy(workspace_id)

        if page_size is not None:
            if page_size < 1:
                raise CliError(
                    "--page-size must be a positive integer.", code="usage", exit_code=EXIT_USAGE
                )
            if server_max is not None and page_size > server_max:
                raise CliError(
                    f"--page-size exceeds the server maximum of {server_max}.",
                    code="usage",
                    exit_code=EXIT_USAGE,
                )
            size = page_size
        else:
            size = min(server_max or DEFAULT_PAGE_SIZE, DEFAULT_PAGE_SIZE)
        return self._snapshot_paged(workspace_id, size)

    def _snapshot_legacy(self, workspace_id: str) -> dict[str, Any]:
        payload = self._json(self._send("GET", f"/api/v1/workspaces/{workspace_id}/sync/snapshot"))
        objects = _require_list(payload.get("objects"), "snapshot")
        return {
            "as_of_seq": _require_decimal(payload.get("as_of_seq"), "snapshot"),
            "min_retained_seq": _require_decimal(payload.get("min_retained_seq"), "snapshot"),
            "objects": objects,
            "has_more": False,
            "next_cursor": None,
            "pages": 1,
            "paged": False,
        }

    def _snapshot_paged(self, workspace_id: str, page_size: int) -> dict[str, Any]:
        path = f"/api/v1/workspaces/{workspace_id}/sync/snapshot"
        as_of: str | None = None
        min_retained: str | None = None
        cursor: str | None = None
        objects: list[Any] = []
        seen: set[tuple[str, str]] = set()

        for page_number in range(1, MAX_PAGES + 1):
            params: dict[str, Any] = {"limit": page_size}
            if cursor is not None:
                params["cursor"] = cursor
            payload = self._json(self._send("GET", path, params=params))

            page_as_of = _require_decimal(payload.get("as_of_seq"), "snapshot page")
            page_floor = _require_decimal(payload.get("min_retained_seq"), "snapshot page")
            if as_of is None:
                as_of = page_as_of
            elif page_as_of != as_of:
                # A watermark that moves mid-bootstrap means the pages do not
                # describe one view; concatenating them would invent a state the
                # server never held.
                raise _invalid("snapshot page watermark")
            min_retained = _decimal_max(min_retained, page_floor)

            page_objects = _require_list(payload.get("objects"), "snapshot page")
            for value in page_objects:
                obj = _require_object(value, "snapshot object")
                key = _object_key(obj)
                if key in seen:
                    raise _invalid("snapshot page (duplicate object)")
                seen.add(key)
                objects.append(obj)

            has_more = _require_bool(payload.get("has_more"), "snapshot page")
            if not has_more:
                return {
                    "as_of_seq": as_of,
                    "min_retained_seq": min_retained,
                    "objects": objects,
                    "has_more": False,
                    "next_cursor": None,
                    "pages": page_number,
                    "paged": True,
                }
            next_cursor = payload.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise _invalid("snapshot page cursor")
            if not page_objects:
                # An empty page that claims more work would loop forever.
                raise _invalid("snapshot page (empty with has_more)")
            cursor = next_cursor

        raise CliError(
            f"The snapshot exceeded {MAX_PAGES} pages; the server is not making progress.",
            code="snapshot_page_budget",
            exit_code=EXIT_API,
        )

    def pull(
        self,
        workspace_id: str,
        *,
        since: str = "0",
        limit: int | None = None,
        drain: bool = True,
    ) -> dict[str, Any]:
        """Read ``(since, ...]``, chaining cursors until the server says done."""
        if not _DECIMAL.fullmatch(since):
            raise CliError(
                "--since must be a canonical unsigned decimal string.",
                code="usage",
                exit_code=EXIT_USAGE,
            )
        if limit is not None and limit < 1:
            raise CliError(
                "--limit must be a positive integer.", code="usage", exit_code=EXIT_USAGE
            )

        path = f"/api/v1/workspaces/{workspace_id}/sync/pull"
        cursor = since
        events: list[Any] = []
        objects: dict[tuple[str, str], Any] = {}
        min_retained: str | None = None
        last_seq: str | None = None
        pages = 0

        for pages in range(1, MAX_PAGES + 1):
            params: dict[str, Any] = {"since": cursor}
            if limit is not None:
                params["limit"] = limit
            payload = self._json(self._send("GET", path, params=params))

            for value in _require_list(payload.get("events"), "pull page"):
                event = _require_object(value, "pull event")
                events.append(event)
            for value in _require_list(payload.get("objects"), "pull page"):
                obj = _require_object(value, "pull object")
                objects[_object_key(obj)] = obj

            page_floor = _require_decimal(payload.get("min_retained_seq"), "pull page")
            min_retained = _decimal_max(min_retained, page_floor)
            last_seq = _require_decimal(payload.get("last_seq"), "pull page")
            cursor = _require_decimal(payload.get("next_cursor"), "pull page")
            has_more = _require_bool(payload.get("has_more"), "pull page")
            if not has_more or not drain:
                return {
                    "since": since,
                    "next_cursor": cursor,
                    "last_seq": last_seq,
                    "min_retained_seq": min_retained,
                    "has_more": has_more,
                    "events": events,
                    "objects": list(objects.values()),
                    "pages": pages,
                }

        raise CliError(
            f"The pull exceeded {MAX_PAGES} pages; the server is not making progress.",
            code="pull_page_budget",
            exit_code=EXIT_API,
        )

    def push(self, workspace_id: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply one batch; per-op outcomes stay in the response."""
        payload = self._json(
            self._send(
                "POST",
                f"/api/v1/workspaces/{workspace_id}/sync/push",
                json_body={"ops": ops},
            )
        )
        results = _require_list(payload.get("results"), "push response")
        for value in results:
            result = _require_object(value, "push result")
            status = result.get("status")
            if status not in _PUSH_STATUSES:
                raise _invalid("push result status")
            _require_text(result.get("op_id"), "push result")
        return payload

    # -- attachments --------------------------------------------------------- #
    def attachment_info(self, attachment_id: str) -> dict[str, Any]:
        return self._json(self._send("GET", f"/api/v1/attachments/{attachment_id}"))

    def attachment_create(
        self,
        workspace_id: str,
        *,
        mime: str,
        byte_size: int,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"mime": mime, "byte_size": byte_size}
        if file_name:
            body["file_name"] = file_name
        payload = self._json(
            self._send(
                "POST",
                f"/api/v1/workspaces/{workspace_id}/attachments",
                json_body=body,
            )
        )
        _require_text(payload.get("attachment_id"), "attachment response")
        upload = _require_object(payload.get("upload"), "attachment upload target")
        _require_text(upload.get("url"), "attachment upload target")
        return payload

    def attachment_upload(
        self,
        workspace_id: str,
        attachment_id: str,
        source: Path,
        *,
        mime: str,
    ) -> dict[str, Any]:
        digest = _sha256(source)
        url = f"/api/v1/workspaces/{workspace_id}/attachments/{attachment_id}/content"
        with source.open("rb") as handle:
            response = self._send(
                "PUT",
                url,
                content=handle,
                headers={
                    "Content-Type": mime,
                    "X-Content-SHA256": digest,
                },
                timeout=300.0,
                # A retried upload would re-read an already-consumed file handle.
                # A 401 here fails honestly; the caller re-runs the command.
                allow_refresh=False,
            )
        return self._json(response)

    def attachment_download(self, attachment_id: str, destination: Path) -> int:
        """Stream one object to ``destination``; returns the byte count written."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        url = f"/api/v1/attachments/{attachment_id}/content"
        written = 0
        response = self._open_stream(url)
        try:
            if response.status_code == 401 and self.credentials.refresh_token:
                response.close()
                self._refresh_tokens()
                response = self._open_stream(url)
            if response.status_code >= 400:
                response.read()
                raise self._api_error(response)
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
                    written += len(chunk)
        finally:
            response.close()
        temporary.replace(destination)
        return written

    def _open_stream(self, path: str) -> httpx.Response:
        """A streaming GET with the current bearer token (caller closes it)."""
        headers = {"Accept": "*/*", **self._auth_headers()}
        try:
            request = self.http.build_request("GET", self._url(path), headers=headers)
            return self.http.send(request, stream=True)
        except httpx.RequestError as error:
            raise CliError(
                f"Could not reach the API at {self.api_base}: {error.__class__.__name__}.",
                code="network_error",
                exit_code=EXIT_API,
            ) from None

    # -- HTTP plumbing ------------------------------------------------------- #
    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    def _auth_headers(self) -> dict[str, str]:
        token = self.credentials.access_token
        if not token:
            raise CliError(
                "Not logged in. Run `revnote login` first.",
                code="not_logged_in",
                exit_code=EXIT_AUTH,
            )
        return {"Authorization": f"Bearer {token}"}

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        content: Any = None,
        headers: Mapping[str, str] | None = None,
        authenticate: bool = True,
        allow_refresh: bool = True,
        timeout: float | None = None,
    ) -> httpx.Response:
        request_headers: dict[str, str] = dict(headers or {})
        if authenticate:
            request_headers.update(self._auth_headers())

        def perform() -> httpx.Response:
            kwargs: dict[str, Any] = {
                "json": json_body,
                "params": params,
                "content": content,
                "headers": request_headers,
            }
            if timeout is not None:
                kwargs["timeout"] = timeout
            return self.http.request(method, self._url(path), **kwargs)

        try:
            response = perform()
        except httpx.RequestError as error:
            raise CliError(
                f"Could not reach the API at {self.api_base}: {error.__class__.__name__}.",
                code="network_error",
                exit_code=EXIT_API,
            ) from None

        if (
            response.status_code == 401
            and authenticate
            and allow_refresh
            and self.credentials.refresh_token
        ):
            # One rotation, one retry. A second 401 is the session's final answer.
            self._refresh_tokens()
            request_headers.update(self._auth_headers())
            try:
                response = perform()
            except httpx.RequestError as error:
                raise CliError(
                    f"Could not reach the API at {self.api_base}: {error.__class__.__name__}.",
                    code="network_error",
                    exit_code=EXIT_API,
                ) from None

        if response.status_code >= 400:
            raise self._api_error(response)
        return response

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            raise _invalid("response (not JSON)") from None
        return _require_object(payload, "response")

    def _api_error(self, response: httpx.Response) -> CliError:
        """Translate the one error envelope into a CliError, keeping scalars only."""
        code: str | None = None
        message = f"The API answered HTTP {response.status_code}."
        request_id: str | None = None
        retryable: bool | None = None
        details: dict[str, Any] = {}
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                raw_code = error.get("code")
                if isinstance(raw_code, str) and raw_code:
                    code = raw_code
                raw_message = error.get("message")
                if isinstance(raw_message, str) and raw_message:
                    message = raw_message
                raw_request = error.get("request_id")
                if isinstance(raw_request, str) and raw_request:
                    request_id = raw_request
                raw_retryable = error.get("retryable")
                if isinstance(raw_retryable, bool):
                    retryable = raw_retryable
                raw_details = error.get("details")
                if isinstance(raw_details, dict):
                    details = {
                        key: value
                        for key, value in raw_details.items()
                        if isinstance(value, (str, int, float, bool))
                    }
        if code is None:
            code = _status_code_name(response.status_code)
        error = CliError(
            message,
            code=code,
            status=response.status_code,
            request_id=request_id,
            retryable=retryable,
            details=details,
        )
        if response.status_code == 401 and error.exit_code != EXIT_AUTH:
            error.exit_code = EXIT_AUTH
        return error

    def _refresh_tokens(self) -> dict[str, Any]:
        refresh_token = self.credentials.refresh_token
        if not refresh_token:
            raise CliError(
                "The session has no refresh token. Run `revnote login` again.",
                code="invalid_refresh",
                exit_code=EXIT_AUTH,
            )
        response = self._send(
            "POST",
            "/api/v1/auth/refresh",
            json_body={"refresh_token": refresh_token},
            authenticate=False,
            allow_refresh=False,
        )
        payload = self._json(response)
        self._adopt_token_pair(payload)
        return payload

    def _adopt_token_pair(self, payload: dict[str, Any]) -> None:
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise _invalid("token response")
        if refresh is not None and not isinstance(refresh, str):
            raise _invalid("token response")
        self.credentials.access_token = access
        self.credentials.refresh_token = refresh
        token_type = payload.get("token_type")
        if isinstance(token_type, str) and token_type:
            self.credentials.token_type = token_type
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, int) and not isinstance(expires_in, bool):
            self.credentials.expires_in = expires_in
        session_id = payload.get("session_id")
        if isinstance(session_id, str):
            self.credentials.session_id = session_id
        session_expires = payload.get("session_expires_at")
        if isinstance(session_expires, str):
            self.credentials.session_expires_at = session_expires
        self.credentials.obtained_at = datetime.now(UTC).isoformat()
        self.store.save(self.credentials)

    def _forget_tokens(self) -> None:
        self.credentials.access_token = None
        self.credentials.refresh_token = None
        self.credentials.session_id = None
        self.credentials.session_expires_at = None
        self.credentials.obtained_at = None


def _object_key(obj: dict[str, Any]) -> tuple[str, str]:
    """``(entity_type, id)`` for de-duplication, validated as the server promises."""
    entity_type = obj.get("entity_type")
    if entity_type not in {"note", "folder"}:
        raise _invalid("object type")
    entity_id = obj.get("id")
    if not isinstance(entity_id, str) or not entity_id:
        raise _invalid("object id")
    return entity_type, entity_id


def _decimal_max(current: str | None, candidate: str) -> str:
    """Larger of two canonical decimals; both are bounded to 19 digits by the regex."""
    if current is None or int(candidate) > int(current):
        return candidate
    return current


def _status_code_name(status: int) -> str:
    if status == 401:
        return "unauthenticated"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status == 413:
        return "payload_too_large"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "internal"
    return "http_error"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guess_mime(path: Path) -> str:
    """Best-effort media type; the server owns the allowlist and may refuse it."""
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
