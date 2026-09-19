"""Tests for the ``add_item`` tool and the client's multipart upload.

Two local aiohttp ``TestServer``s stand in for the world: a fake Wardrowbe
backend (``/api/v1/auth/sync`` + ``/api/v1/items``) and a fake shop serving a
product page with ``og:image``. The tool is invoked through FastMCP's
``call_tool``. Async code runs under ``asyncio.run`` (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp.server.fastmcp.exceptions import ToolError

from wardrowbe_mcp import server as server_module
from wardrowbe_mcp.auth import DevTokenProvider
from wardrowbe_mcp.client import WardrowbeClient
from wardrowbe_mcp.image_source import resolve_image
from wardrowbe_mcp.server import build_mcp_server

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 50
ITEM_ID = "0b6f3c2e-1111-4222-8333-944455556666"
DUP_ID = "7d0e2a41-aaaa-4bbb-8ccc-dddd00001111"
INVALID_DETAIL = "Invalid image file. Supported formats: JPEG, PNG, WebP, HEIC"


@pytest.fixture(autouse=True)
def _allow_loopback_shop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake shop serves from 127.0.0.1, which the fetch guard refuses.

    Opt in the way the image_source tests do, at the tool's call site, so the
    server code itself never passes ``allow_private_hosts``.
    """
    monkeypatch.setattr(
        server_module,
        "resolve_image",
        functools.partial(resolve_image, allow_private_hosts=True),
    )


@dataclass
class Backend:
    """State shared with the fake backend's handlers."""

    syncs: int = 0
    item_posts: int = 0
    fail_first_with_401: bool = False
    uploads: list[dict[str, Any]] = field(default_factory=list)


def _backend_app(state: Backend) -> web.Application:
    async def sync(request: web.Request) -> web.Response:
        state.syncs += 1
        return web.json_response(
            {"access_token": f"jwt-{state.syncs}", "expires_in": 3600}
        )

    async def create_item(request: web.Request) -> web.Response:
        state.item_posts += 1
        if not request.content_type.startswith("multipart/form-data"):
            return web.json_response({"detail": "expected multipart"}, status=422)
        form = await request.post()
        image = form.get("image")
        upload = {
            "authorization": request.headers.get("Authorization"),
            "fields": {k: v for k, v in form.items() if k != "image"},
            "image_bytes": image.file.read() if image is not None else None,
            "image_filename": getattr(image, "filename", None),
            "image_content_type": getattr(image, "content_type", None),
        }
        state.uploads.append(upload)
        if state.fail_first_with_401 and state.item_posts == 1:
            return web.json_response({"detail": "token expired"}, status=401)
        if form.get("name") == "big":
            return web.Response(status=413, text="<html>413 Request Entity Too Large</html>")
        if form.get("name") == "boom":
            return web.Response(status=500, text="x" * 1000)
        if form.get("name") == "bad":
            return web.json_response({"detail": INVALID_DETAIL}, status=400)
        if form.get("name") == "dup":
            return web.json_response(
                {
                    "detail": "Duplicate image detected. This item already "
                    f"exists in your wardrobe (ID: {DUP_ID})"
                },
                status=409,
            )
        return web.json_response(
            {"id": ITEM_ID, "status": "processing", "name": form.get("name")},
            status=201,
        )

    app = web.Application()
    app.router.add_post("/api/v1/auth/sync", sync)
    app.router.add_post("/api/v1/items", create_item)
    return app


def _shop_app() -> web.Application:
    async def page(request: web.Request) -> web.Response:
        base = str(request.url.origin())
        html = (
            "<!doctype html><html><head><title>Navy Oxford Shirt</title>"
            f'<meta property="og:image" content="{base}/cdn/navy.jpg">'
            "</head><body>shop</body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    async def image(request: web.Request) -> web.Response:
        return web.Response(body=JPEG, content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/products/navy-oxford", page)
    app.router.add_get("/cdn/navy.jpg", image)
    return app


def _call_add_item(
    state: Backend, *, allow_local_files: bool = True, **arguments: Any
) -> tuple[Any, str]:
    """Run ``add_item`` against both fake servers.

    ``arguments["source"]`` may contain ``{shop}``, replaced with the shop's
    base URL. ``allow_local_files`` defaults to stdio-mode behaviour.
    Returns ``(structured_result, shop_base)``.
    """

    async def run() -> tuple[Any, str]:
        backend = TestServer(_backend_app(state))
        shop = TestServer(_shop_app())
        await backend.start_server()
        await shop.start_server()
        try:
            shop_base = str(shop.make_url("")).rstrip("/")
            args = dict(arguments)
            args["source"] = args["source"].format(shop=shop_base)
            async with aiohttp.ClientSession() as session:
                client = WardrowbeClient(
                    session,
                    str(backend.make_url("")),
                    DevTokenProvider("tester"),
                )
                mcp = build_mcp_server(client, allow_local_files=allow_local_files)
                result = await mcp.call_tool("add_item", args)
            structured = result[1] if isinstance(result, tuple) else result
            return structured, shop_base
        finally:
            await shop.close()
            await backend.close()

    return asyncio.run(run())


def test_add_item_from_product_page_uploads_multipart_and_returns_shape() -> None:
    state = Backend()
    result, shop = _call_add_item(
        state,
        source="{shop}/products/navy-oxford",
        name="Navy Oxford shirt",
        brand="Uniqlo",
        notes="bought online",
        favorite=True,
    )

    assert result == {
        "item_id": ITEM_ID,
        "status": "processing",
        "image_url": f"{shop}/cdn/navy.jpg",
        "page_title": "Navy Oxford Shirt",
        "message": "Added to your wardrobe; AI tagging is in progress.",
    }
    assert len(state.uploads) == 1
    upload = state.uploads[0]
    assert upload["authorization"] == "Bearer jwt-1"
    assert upload["image_bytes"] == JPEG
    assert upload["image_filename"] == "navy.jpg"
    assert upload["image_content_type"] == "image/jpeg"
    assert upload["fields"] == {
        "name": "Navy Oxford shirt",
        "brand": "Uniqlo",
        "notes": "bought online",
        "favorite": "true",
    }


def test_optional_fields_are_omitted_and_favorite_defaults_false() -> None:
    state = Backend()
    _call_add_item(state, source="{shop}/cdn/navy.jpg")

    assert state.uploads[0]["fields"] == {"favorite": "false"}
    assert state.uploads[0]["image_bytes"] == JPEG


def test_duplicate_409_is_reported_as_already_in_wardrobe() -> None:
    state = Backend()
    with pytest.raises(ToolError) as excinfo:
        _call_add_item(state, source="{shop}/cdn/navy.jpg", name="dup")

    message = str(excinfo.value)
    assert "Already in your wardrobe. Duplicate image detected." in message
    assert DUP_ID in message
    assert "→ 409" not in message  # the detail, not the raw client error


def test_backend_400_passes_the_detail_through() -> None:
    state = Backend()
    with pytest.raises(ToolError) as excinfo:
        _call_add_item(state, source="{shop}/cdn/navy.jpg", name="bad")

    message = str(excinfo.value)
    assert message.endswith(f": {INVALID_DETAIL}")
    assert "→ 400" not in message
    assert '{"detail"' not in message


def test_401_on_first_upload_resyncs_and_resends_a_fresh_body() -> None:
    state = Backend(fail_first_with_401=True)
    result, _ = _call_add_item(
        state, source="{shop}/cdn/navy.jpg", name="Navy Oxford shirt"
    )

    assert result["item_id"] == ITEM_ID
    assert state.syncs == 2
    assert state.item_posts == 2
    retry = state.uploads[1]
    assert retry["authorization"] == "Bearer jwt-2"
    assert retry["image_bytes"] == JPEG  # full body, not an exhausted FormData
    assert retry["fields"] == {"name": "Navy Oxford shirt", "favorite": "false"}


def test_image_source_error_is_surfaced_verbatim() -> None:
    state = Backend()
    with pytest.raises(ToolError) as excinfo:
        _call_add_item(state, source="ftp://example.com/shirt.jpg")

    assert state.item_posts == 0
    assert str(excinfo.value) == (
        "Error executing tool add_item: No such file: ftp://example.com/shirt.jpg"
    )


def test_local_file_is_refused_unless_the_server_allows_it(tmp_path: Path) -> None:
    photo = tmp_path / "shirt.jpg"
    photo.write_bytes(JPEG)
    state = Backend()
    with pytest.raises(ToolError) as excinfo:
        _call_add_item(state, source=str(photo), allow_local_files=False)

    assert state.item_posts == 0
    assert str(excinfo.value) == (
        "Error executing tool add_item: Local file paths only work when the server "
        "runs on your own machine. Paste a product page or image link."
    )


def test_backend_413_says_the_upload_was_too_large() -> None:
    state = Backend()
    with pytest.raises(ToolError) as excinfo:
        _call_add_item(state, source="{shop}/cdn/navy.jpg", name="big")

    size = f"{len(JPEG) / (1024 * 1024):.1f}"
    assert str(excinfo.value) == (
        "Error executing tool add_item: Wardrowbe, or a proxy in front of it, "
        f"refused the upload as too large ({size} MB). "
        "Raise the proxy's body-size limit."
    )


def test_other_backend_errors_truncate_the_raw_body() -> None:
    state = Backend()
    with pytest.raises(ToolError) as excinfo:
        _call_add_item(state, source="{shop}/cdn/navy.jpg", name="boom")

    message = str(excinfo.value)
    assert "→ 500" in message
    assert "x" * 300 in message
    assert "x" * 301 not in message
