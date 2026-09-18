# Changelog

## [0.3.1] — 2026-09-18

### Features

- `--email` / `MCP_EMAIL` and `--display-name` / `MCP_DISPLAY_NAME` for
  `--auth dev`. Previously the server always sent the synthesised
  `<external_id>@wardrowbe.local` and reused the external_id as the display
  name; because the backend's `/auth/sync` overwrites both stored fields on
  every sync, pointing the server at an existing web-login user replaced
  that user's real email each session. With neither flag set the payload is
  byte-for-byte what 0.3.0 sent. Ignored in OIDC mode, where the claims
  supply both values.

### Development

- `pip install -e ".[dev]"` (or `uv sync --extra dev`) installs pytest;
  `tests/test_cli.py` covers the dev-mode identity flags.

## [0.3.0] — 2026-05-27

### Features

- `--transport stdio` (or `MCP_TRANSPORT=stdio`) runs the server as a
  stdio MCP child for bridges like `sparfenyuk/mcp-proxy` and the
  `HASS-MCPProxy` add-on. Default remains `http` (no behaviour change for
  existing deployments). In stdio mode, Starlette/uvicorn and the Bearer
  middleware are skipped — the parent process owns the trust boundary —
  but backend auth (`--auth dev|oidc`) still applies.
- Logging now writes to stderr explicitly so stdout stays clean for the
  JSON-RPC frame stream.

## [0.2.0] — 2026-05-25

Initial standalone release, extracted from
[`ha-wardrowbe`](https://github.com/saya6k/ha-wardrowbe)'s bundled MCP
server so it can be installed and run independently of the Home Assistant
add-on.

### Features

- 22 MCP tools mirroring `hacs-wardrowbe/llm_api/` one-for-one, plus three
  read-only helpers (`list_items`, `get_item`, `get_outfit`) that don't
  fit the HA satellite-card envelope.
- Dual transport on one port: Streamable HTTP (`/mcp`) and SSE (`/sse`).
- `BearerAuthMiddleware` gates every non-probe route; anonymous health at
  `/` and `/health` for watchdogs.
- Two backend auth modes: dev-login sync (`/auth/sync`) and OIDC refresh
  token (auto-rotates on response).
- Bundled agentskills.io-format skill (`SKILL.md` + worked examples)
  registered as MCP resources at `skill://wardrowbe-skill/*` for clients
  that auto-install skills.
- DNS rebinding protection disabled by default in the SDK because the
  Bearer middleware already gates inbound traffic and the SDK's default
  allowlist (127.0.0.1 + localhost) rejects cross-container Host headers.
