# Changelog

## [0.4.0] — 2026-09-19

### Features

- `add_item(source, name, brand, notes, favorite)` creates a wardrobe item
  from a picture, bringing the tool count to 23. `source` is a retailer
  product page URL, a direct image URL, or (stdio transport only) a file
  path on the machine running this server. A product page's image comes
  from `og:image`, then `twitter:image`, then the JSON-LD `Product`'s
  `image` (a string, a list, an `ImageObject`, or an `@id` reference to
  one, as WooCommerce and Shopify themes publish it). The image is uploaded
  to `POST /api/v1/items` and the backend runs AI tagging, so the new item
  usually starts in `processing`. Only http/https URLs are fetched, images
  are capped at 10 MB, and JPEG, PNG, WebP or HEIC is expected. A duplicate
  (409) is reported as "Already in your wardrobe."
- Shop fetches use `curl_cffi` impersonating Chrome, because
  Cloudflare-fronted shops (Loake, J.Crew) answer plain Python HTTP clients
  with 403 by TLS fingerprint, regardless of headers. `curl_cffi>=0.9` is a
  new required dependency; as of 2026-09-19 it ships binary wheels for
  win_amd64, macOS x86_64 and arm64, and manylinux and musllinux x86_64 and
  aarch64. The Wardrowbe backend is still reached with aiohttp.

### Security

- `add_item` resolves every host before fetching it and refuses any that
  resolves to a non-public address (loopback, private, link-local, CGNAT,
  multicast, and so on), on redirects too, which are followed by hand (at
  most 5 hops, http/https only). Known gap: a DNS record that changes
  between this check and curl's own lookup (DNS rebinding) is not caught;
  pinning the resolved address in curl was judged too heavy for this tool.
- Local file paths are accepted only with `--transport stdio`. Over HTTP
  the tool answers "Local file paths only work when the server runs on your
  own machine."
- A product link that redirects to a different page (a category, the home
  page) is refused, since that page's image is usually the site logo. Both
  URLs are normalised the same way first (percent-decoding, dot segments,
  trailing slash, case), and a redirect that only adds or drops a locale
  prefix (for example `/uk/`) counts as the same page.
- The address check also refuses IPv6 addresses that carry a non-public
  IPv4 address (IPv4-mapped, IPv4-compatible, 6to4, Teredo, NAT64), and the
  whole URL resolution is capped at 60 seconds.

### Development

- `tests/test_image_source.py`, `tests/test_add_item.py` and
  `tests/test_cli.py` cover the source resolver, the fetch guards, the
  JSON-LD shapes, the upload path and the per-transport file gating. Local
  test servers opt into loopback with `allow_private_hosts=True`.

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
