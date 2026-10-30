"""The reference CLI against a real API, a real PostgreSQL schema and a real socket.

Why a subprocess
----------------
``revnote`` is a synchronous console program; the API is an asyncio server served
by uvicorn on a loopback socket (:mod:`pg_support.socket_probe`). Running the CLI
as a child process is the honest arrangement: the server keeps its event loop, the
CLI gets its own, and the test exercises exactly ``python -m revnote_cli`` rather
than a re-implementation of argument parsing. A blocking in-process call would
stall the server it is calling.

Every case seeds its own account and workspace through the shared
``pg_support`` harness, so the suite is sequential and independent. A missing
database is a hard failure, never a skip.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from pg_support.api_harness import folder_create, note_create, open_app
from pg_support.socket_probe import Probe, open_probe

pytestmark = [pytest.mark.anyio, pytest.mark.cli]

REPO_ROOT = Path(__file__).resolve().parents[4]


class Cli:
    """One isolated CLI configuration directory pointed at the probe."""

    def __init__(self, probe: Probe, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.env = os.environ.copy()
        self.env["REVNOTE_API_BASE"] = probe.base_url
        self.env["REVNOTE_CONFIG_DIR"] = str(config_dir)

    async def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return await asyncio.to_thread(_run, self.env, args)

    async def json(self, *args: str) -> Any:
        result = await self.run(*args, "--json")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def saved(self) -> dict[str, Any]:
        return json.loads((self.config_dir / "config.json").read_text(encoding="utf-8"))


def _run(env: dict[str, str], args: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "revnote_cli", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


async def _token(probe: Probe, email: str, password: str) -> str:
    async with httpx.AsyncClient(base_url=probe.base_url, timeout=30.0) as http:
        response = await http.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password, "device_name": "cli-test-seed"},
        )
        assert response.status_code == 200, response.text
        return str(response.json()["access_token"])


async def _push(probe: Probe, token: str, workspace_id: uuid.UUID, ops: list[dict[str, Any]]):
    async with httpx.AsyncClient(base_url=probe.base_url, timeout=30.0) as http:
        response = await http.post(
            f"/api/v1/workspaces/{workspace_id}/sync/push",
            json={"ops": ops},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert all(item["status"] == "applied" for item in payload["results"]), payload
        return payload


async def test_cli_login_me_tree_logout_keep_tokens_in_the_config_dir(tmp_path: Path) -> None:
    async with open_app() as app, open_probe(app.app) as probe:
        token = await _token(probe, app.account.email, app.account.password)
        seeded = note_create(title="Seeded")
        await _push(probe, token, app.workspace_id, [seeded])
        note_id = seeded["entity_id"]

        cli = Cli(probe, tmp_path / "config")
        login = await cli.run(
            "login", "--email", app.account.email, "--password", app.account.password, "--json"
        )
        assert login.returncode == 0, login.stderr
        payload = json.loads(login.stdout)
        assert payload["workspace_id"] == str(app.workspace_id)

        saved = cli.saved()
        access_token = saved["access_token"]
        assert access_token not in login.stdout
        assert access_token not in login.stderr
        assert sorted(path.name for path in cli.config_dir.iterdir()) == ["config.json"]

        identity = await cli.json("me")
        assert identity["user"]["id"] == str(app.user_id)

        tree = await cli.json("tree")
        ids = {item["id"] for item in tree["notes"]}
        assert note_id in ids

        logout = await cli.run("logout")
        assert logout.returncode == 0, logout.stderr
        assert not (cli.config_dir / "config.json").exists()

        after = await cli.run("me")
        assert after.returncode == 3
        assert "not_logged_in" in after.stderr


async def test_cli_paged_snapshot_collects_every_object_and_pins_the_watermark(
    tmp_path: Path,
) -> None:
    async with open_app() as app, open_probe(app.app) as probe:
        token = await _token(probe, app.account.email, app.account.password)
        folders = [folder_create(name=f"F{index}") for index in range(2)]
        notes = [note_create(title=f"N{index}", body_md=f"body {index}") for index in range(7)]
        await _push(probe, token, app.workspace_id, folders + notes)

        cli = Cli(probe, tmp_path / "config")
        await cli.json("login", "--email", app.account.email, "--password", app.account.password)
        snapshot = await cli.json("sync", "snapshot", "--page-size", "3")

        objects = snapshot["objects"]
        ids = {item["id"] for item in objects}
        assert len(objects) == 9
        assert snapshot["paged"] is True
        assert snapshot["pages"] >= 3
        assert {item["entity_id"] for item in folders + notes} <= ids
        assert all(int(item["updated_seq"]) <= int(snapshot["as_of_seq"]) for item in objects)
        assert len(ids) == len(objects)

        # The pinned watermark is exactly where the incremental cursor starts.
        empty = await cli.json("sync", "pull", "--since", snapshot["as_of_seq"])
        assert empty["events"] == []
        assert empty["next_cursor"] == snapshot["as_of_seq"]
        assert empty["last_seq"] == snapshot["as_of_seq"]


async def test_cli_pull_returns_the_increment_after_the_snapshot(tmp_path: Path) -> None:
    async with open_app() as app, open_probe(app.app) as probe:
        token = await _token(probe, app.account.email, app.account.password)
        await _push(probe, token, app.workspace_id, [note_create(title="Before")])

        cli = Cli(probe, tmp_path / "config")
        await cli.json("login", "--email", app.account.email, "--password", app.account.password)
        snapshot = await cli.json("sync", "snapshot")
        watermark = snapshot["as_of_seq"]

        await _push(
            probe,
            token,
            app.workspace_id,
            [note_create(title="After one"), note_create(title="After two")],
        )

        increment = await cli.json("sync", "pull", "--since", watermark)
        assert len(increment["events"]) == 2
        assert len(increment["objects"]) == 2
        assert all(int(event["seq"]) > int(watermark) for event in increment["events"])
        assert increment["next_cursor"] == increment["last_seq"]
        assert int(increment["last_seq"]) == int(watermark) + 2


async def test_cli_push_note_create_and_update_round_trip(tmp_path: Path) -> None:
    async with open_app() as app, open_probe(app.app) as probe:
        cli = Cli(probe, tmp_path / "config")
        await cli.json("login", "--email", app.account.email, "--password", app.account.password)

        created = await cli.json(
            "sync", "push", "note", "create", "--title", "CLI note", "--body-text", "first"
        )
        note_id = created["created_note_id"]
        assert created["results"][0]["status"] == "applied"
        assert created["results"][0]["rev"] == "1"

        read = await cli.json("note", "get", note_id)
        assert read["id"] == note_id
        assert read["title"] == "CLI note"
        assert read["body_md"] == "first"
        assert read["rev"] == "1"

        updated = await cli.json(
            "sync",
            "push",
            "note",
            "update",
            note_id,
            "--base-rev",
            "1",
            "--title",
            "CLI note",
            "--body-text",
            "second",
        )
        assert updated["results"][0]["status"] == "applied"
        assert updated["results"][0]["rev"] == "2"

        reread = await cli.json("note", "get", note_id)
        assert reread["body_md"] == "second"
        assert reread["rev"] == "2"


async def test_cli_push_conflict_exits_four_and_leaves_the_note_untouched(
    tmp_path: Path,
) -> None:
    async with open_app() as app, open_probe(app.app) as probe:
        cli = Cli(probe, tmp_path / "config")
        await cli.json("login", "--email", app.account.email, "--password", app.account.password)
        chosen = str(uuid.uuid4())
        created = await cli.json(
            "sync",
            "push",
            "note",
            "create",
            "--entity-id",
            chosen,
            "--title",
            "Conflict",
            "--body-text",
            "original",
        )
        assert created["created_note_id"] == chosen

        conflict = await cli.run(
            "sync",
            "push",
            "note",
            "update",
            chosen,
            "--base-rev",
            "2",
            "--title",
            "Conflict",
            "--body-text",
            "never",
            "--json",
        )
        assert conflict.returncode == 4, conflict.stderr
        stdout = json.loads(conflict.stdout)
        assert stdout["results"][0]["status"] == "conflict"
        stderr = json.loads(conflict.stderr)
        assert stderr["error"]["code"] == "rev_conflict"

        unchanged = await cli.json("note", "get", chosen)
        assert unchanged["rev"] == "1"
        assert unchanged["body_md"] == "original"


async def test_cli_capabilities_are_available_without_a_session(tmp_path: Path) -> None:
    async with open_app() as app, open_probe(app.app) as probe:
        cli = Cli(probe, tmp_path / "never-logged-in")
        payload = await cli.json("capabilities")
        assert payload["snapshot_pagination"]["enabled"] is True
        assert payload["snapshot_pagination"]["page_max_objects"] >= 1
        assert payload["joplin_bridge"]["enabled"] is False
        assert "enabled" in payload["attachments"]
        assert not cli.config_dir.exists()


async def test_cli_me_without_a_session_exits_three(tmp_path: Path) -> None:
    async with open_app() as app, open_probe(app.app) as probe:
        cli = Cli(probe, tmp_path / "never-logged-in")
        result = await cli.run("me")
        assert result.returncode == 3
        assert "not_logged_in" in result.stderr
