"""Credential storage: directory precedence, round trip, and no token in repr."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from revnote_cli.config import (
    CredentialStore,
    StoredCredentials,
    resolve_config_dir,
)

pytestmark = pytest.mark.cli


def test_config_dir_precedence_is_flag_then_env_then_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("REVNOTE_CONFIG_DIR", str(tmp_path / "env"))
    assert resolve_config_dir(str(tmp_path / "flag")) == tmp_path / "flag"
    assert resolve_config_dir(None) == tmp_path / "env"
    monkeypatch.delenv("REVNOTE_CONFIG_DIR")
    assert resolve_config_dir(None) != tmp_path / "env"


def test_store_round_trips_and_repr_never_prints_tokens(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "config")
    credentials = StoredCredentials(
        api_base="http://api.test",
        access_token="access-sentinel-value",
        refresh_token="refresh-sentinel-value",
        expires_in=3600,
        obtained_at=datetime.now(UTC).isoformat(),
        session_id="session-1",
        workspace_id="workspace-1",
        user_id="user-1",
    )
    store.save(credentials)

    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "access-sentinel-value"
    assert loaded.refresh_token == "refresh-sentinel-value"
    assert loaded.workspace_id == "workspace-1"

    rendered = repr(loaded)
    assert "access-sentinel-value" not in rendered
    assert "refresh-sentinel-value" not in rendered
    assert "present" in rendered


def test_corrupt_or_missing_file_reads_as_not_logged_in(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "config")
    assert store.load() is None
    store.directory.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() is None


def test_clear_is_idempotent(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "config")
    store.save(StoredCredentials(api_base="http://api.test"))
    assert store.clear() is True
    assert store.clear() is False


def test_expiry_is_derived_from_obtained_at_and_expires_in() -> None:
    expired = StoredCredentials(
        expires_in=60,
        obtained_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    )
    assert expired.is_expiring() is True
    fresh = StoredCredentials(
        expires_in=3600,
        obtained_at=datetime.now(UTC).isoformat(),
    )
    assert fresh.is_expiring() is False
    assert StoredCredentials().is_expiring() is True
