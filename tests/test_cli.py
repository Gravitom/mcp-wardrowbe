"""CLI tests for wardrowbe_mcp.__main__.

These exercise argparse → DevTokenProvider → /auth/sync payload without any
network: in dev mode ``_build_token_provider`` ignores the aiohttp session,
so ``None`` is passed for it.
"""

from __future__ import annotations

import asyncio

import pytest

from wardrowbe_mcp.__main__ import _build_argparser, _build_token_provider

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
