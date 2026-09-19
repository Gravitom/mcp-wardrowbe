"""CLI tests for wardrowbe_mcp.__main__.

These exercise argparse → DevTokenProvider → /auth/sync payload without any
network: in dev mode ``_build_token_provider`` ignores the aiohttp session,
so ``None`` is passed for it.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from starlette.applications import Starlette

import wardrowbe_mcp.__main__ as entry
from wardrowbe_mcp.__main__ import _build_argparser, _build_token_provider
from wardrowbe_mcp.server import build_mcp_server

_IDENTITY_ENV = ("MCP_EMAIL", "MCP_DISPLAY_NAME", "MCP_EXTERNAL_ID", "MCP_AUTH_MODE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argparse defaults read os.environ at parser-build time; start clean."""
    for name in _IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)


def _sync_payload(argv: list[str]) -> dict[str, str]:
    args = _build_argparser().parse_args(argv)
    provider = _build_token_provider(args, session=None)  # type: ignore[arg-type]
    return asyncio.run(provider.async_get_sync_payload())


def test_defaults_are_unchanged_from_0_3_0() -> None:
    payload = _sync_payload(["--auth", "dev", "--external-id", "abc"])
    assert payload == {
        "external_id": "abc",
        "email": "abc@wardrowbe.local",
        "display_name": "abc",
    }


def test_email_and_display_name_flags_reach_the_payload() -> None:
    payload = _sync_payload(
        [
            "--auth", "dev",
            "--external-id", "gravitom-gmail-com",
            "--email", "gravitom@gmail.com",
            "--display-name", "Tom",
        ]
    )
    assert payload == {
        "external_id": "gravitom-gmail-com",
        "email": "gravitom@gmail.com",
        "display_name": "Tom",
    }


def test_env_vars_behave_like_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EXTERNAL_ID", "gravitom-gmail-com")
    monkeypatch.setenv("MCP_EMAIL", "gravitom@gmail.com")
    monkeypatch.setenv("MCP_DISPLAY_NAME", "Tom")
    payload = _sync_payload(["--auth", "dev"])
    assert payload == {
        "external_id": "gravitom-gmail-com",
        "email": "gravitom@gmail.com",
        "display_name": "Tom",
    }


def test_flag_wins_over_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EMAIL", "env@example.com")
    monkeypatch.setenv("MCP_DISPLAY_NAME", "Env")
    payload = _sync_payload(
        ["--auth", "dev", "--external-id", "abc",
         "--email", "flag@example.com", "--display-name", "Flag"]
    )
    assert payload["email"] == "flag@example.com"
    assert payload["display_name"] == "Flag"


# --- add_item local-file gating per transport ---------------------------------


class _FakeMcp:
    """Stands in for FastMCP: no transport is actually started."""

    async def run_stdio_async(self) -> None:
        return None

    def sse_app(self) -> Starlette:
        return Starlette()

    def streamable_http_app(self) -> Starlette:
        return Starlette()


class _FakeUvicornServer:
    def __init__(self, config: Any) -> None:
        self.config = config

    async def serve(self) -> None:
        return None


def _capture_build(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_build(client: Any, name: str = "wardrowbe", **kwargs: Any) -> _FakeMcp:
        captured.update(kwargs)
        return _FakeMcp()

    monkeypatch.setattr(entry, "build_mcp_server", fake_build)
    return captured


def test_stdio_transport_allows_local_files(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_build(monkeypatch)
    args = _build_argparser().parse_args(["--transport", "stdio", "--auth", "dev"])
    asyncio.run(entry._serve_stdio(args))
    assert captured == {"allow_local_files": True}


def test_http_transport_refuses_local_files(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_build(monkeypatch)
    monkeypatch.setattr(entry.uvicorn, "Server", _FakeUvicornServer)
    args = _build_argparser().parse_args(["--transport", "http", "--auth", "dev", "--api-key", "k"])
    asyncio.run(entry._serve_http(args))
    assert captured == {"allow_local_files": False}


def test_build_mcp_server_refuses_local_files_by_default() -> None:
    assert inspect.signature(build_mcp_server).parameters["allow_local_files"].default is False
