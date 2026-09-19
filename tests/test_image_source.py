"""Tests for wardrowbe_mcp.image_source.

A local aiohttp ``TestServer`` plays the shop, so nothing touches the
internet. The resolver fetches it with curl_cffi over loopback, which the
tests opt into with ``allow_private_hosts=True`` (the server never does).
Async code runs under ``asyncio.run`` (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wardrowbe_mcp import image_source
from wardrowbe_mcp.image_source import ImageSourceError, ResolvedImage, resolve_image

PRIVATE = "That link points to a private or local network address, which I won't fetch."

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 10
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"fake-webp-body" * 10


def _page(head: str, title: str = "Navy Oxford Shirt") -> str:
    return f"<!doctype html><html><head><title>{title}</title>{head}</head><body>hi</body></html>"


def _image(body: bytes = JPEG, content_type: str = "image/jpeg") -> Callable:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(body=body, content_type=content_type)

    return handler


def _html(text: str) -> Callable:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=text, content_type="text/html")

    return handler


def _resolve(routes: dict[str, Callable], path: str) -> ResolvedImage:
    """Serve ``routes`` on a local TestServer and resolve ``path`` against it.

    ``path`` is appended to the server's base URL. URLs never need
    ``allow_local_files``, so it is False here, as in HTTP mode.
    """

    async def run() -> ResolvedImage:
        app = web.Application()
        for route, handler in routes.items():
            app.router.add_get(route, handler)
        server = TestServer(app)
        await server.start_server()
        try:
            base = str(server.make_url("")).rstrip("/")
            return await resolve_image(
                f"{base}{path}", allow_local_files=False, allow_private_hosts=True
            )
        finally:
            await server.close()

    return asyncio.run(run())


def _page_with_base(head_template: str, title: str = "Navy Oxford Shirt") -> Callable:
    """Page handler whose ``head_template`` can reference ``{base}``."""

    async def handler(request: web.Request) -> web.Response:
        head = head_template.format(base=str(request.url.origin()))
        return web.Response(text=_page(head, title), content_type="text/html")

    return handler


# --- URLs -----------------------------------------------------------------


def test_direct_image_link() -> None:
    result = _resolve({"/img/shirt.jpg": _image()}, "/img/shirt.jpg")
    assert result.data == JPEG
    assert result.content_type == "image/jpeg"
    assert result.filename == "shirt.jpg"
    assert result.image_url is not None and result.image_url.endswith("/img/shirt.jpg")
    assert result.page_title is None


def test_image_request_asks_for_jpeg_png_webp_with_browser_user_agent() -> None:
    seen: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["accept"] = request.headers.get("Accept", "")
        seen["ua"] = request.headers.get("User-Agent", "")
        return web.Response(body=PNG, content_type="image/png")

    page = _page_with_base('<meta property="og:image" content="{base}/cdn/p">')
    _resolve({"/p/shirt": page, "/cdn/p": handler}, "/p/shirt")
    assert seen["accept"] == "image/jpeg,image/png,image/webp;q=0.9,*/*;q=0.5"
    assert "Mozilla/5.0" in seen["ua"]


def test_page_with_og_image() -> None:
    page = _page_with_base(
        '<meta property="og:image" content="{base}/cdn/navy.png">'
        '<meta name="twitter:image" content="{base}/cdn/wrong.jpg">'
    )
    result = _resolve(
        {"/p/shirt": page, "/cdn/navy.png": _image(PNG, "image/png")}, "/p/shirt"
    )
    assert result.data == PNG
    assert result.content_type == "image/png"
    assert result.filename == "navy.png"
    assert result.image_url is not None and result.image_url.endswith("/cdn/navy.png")


def test_page_with_only_twitter_image() -> None:
    page = _page_with_base('<meta name="twitter:image" content="{base}/cdn/tw.jpg">')
    result = _resolve({"/p/shirt": page, "/cdn/tw.jpg": _image()}, "/p/shirt")
    assert result.data == JPEG
    assert result.image_url is not None and result.image_url.endswith("/cdn/tw.jpg")


def test_og_image_with_name_attribute_variant() -> None:
    page = _page_with_base('<meta name="og:image" content="{base}/cdn/n.jpg">')
    result = _resolve({"/p/shirt": page, "/cdn/n.jpg": _image()}, "/p/shirt")
    assert result.data == JPEG


def test_relative_og_image_is_resolved_against_the_page_url() -> None:
    page = _html(_page('<meta property="og:image" content="../cdn/rel.jpg">'))
    result = _resolve({"/p/shirt": page, "/cdn/rel.jpg": _image()}, "/p/shirt")
    assert result.data == JPEG
    assert result.image_url is not None and result.image_url.endswith("/cdn/rel.jpg")


def test_page_title_is_returned() -> None:
    page = _html(_page('<meta property="og:image" content="/cdn/a.jpg">', "Shop &amp; Co"))
    result = _resolve({"/p/shirt": page, "/cdn/a.jpg": _image()}, "/p/shirt")
    assert result.page_title == "Shop & Co"


def test_og_title_wins_over_title_tag() -> None:
    page = _html(
        _page(
            '<meta property="og:title" content="Navy Oxford">'
            '<meta property="og:image" content="/cdn/a.jpg">',
            "Navy Oxford | Big Shop",
        )
    )
    result = _resolve({"/p/shirt": page, "/cdn/a.jpg": _image()}, "/p/shirt")
    assert result.page_title == "Navy Oxford"


def test_page_without_an_image() -> None:
    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/p/shirt": _html(_page(""))}, "/p/shirt")
    assert str(exc.value) == (
        "No product image found on that page. Paste a direct image link or a file path."
    )


def test_shop_blocks_the_download_with_403() -> None:
    async def forbidden(request: web.Request) -> web.Response:
        return web.Response(status=403, text="nope")

    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/p/shirt": forbidden}, "/p/shirt")
    assert str(exc.value) == (
        "The shop blocked the download (HTTP 403). "
        "Save the image from your browser and give me the file path."
    )


def test_non_image_content_type() -> None:
    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/file": _image(b"%PDF-1.4", "application/pdf")}, "/file")
    assert str(exc.value) == (
        "That link returned application/pdf, not an image. "
        "Wardrowbe accepts JPEG, PNG, WebP or HEIC."
    )


def test_og_image_pointing_at_another_page_is_not_followed() -> None:
    page = _html(_page('<meta property="og:image" content="/p/other">'))
    other = _html(_page('<meta property="og:image" content="/cdn/a.jpg">'))
    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/p/shirt": page, "/p/other": other, "/cdn/a.jpg": _image()}, "/p/shirt")
    assert "returned text/html, not an image" in str(exc.value)


def test_body_over_the_limit_by_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_source, "_MAX_BYTES", 1000)
    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/big.jpg": _image(b"x" * 5000)}, "/big.jpg")
    assert str(exc.value).startswith("Image is ")
    assert "MB; Wardrowbe's limit is " in str(exc.value)


def test_body_over_the_limit_without_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_source, "_MAX_BYTES", 1000)

    async def chunked(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for _ in range(10):
            await resp.write(b"x" * 500)
        await resp.write_eof()
        return resp

    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/big.jpg": chunked}, "/big.jpg")
    assert str(exc.value).startswith("Image is ")


def test_limit_message_uses_megabytes() -> None:
    assert image_source._too_big(15 * 1024 * 1024) == (
        "Image is 15.0 MB; Wardrowbe's limit is 10 MB."
    )


# --- JSON-LD ------------------------------------------------------------------


def _ldjson(payload: object) -> str:
    return f'<script type="application/ld+json">{json.dumps(payload)}</script>'


def test_jsonld_product_with_string_image() -> None:
    page = _html(_page(_ldjson({"@context": "https://schema.org", "@type": "Product",
                                "name": "Boot", "image": "/cdn/boot.jpg"})))
    result = _resolve({"/p/boot": page, "/cdn/boot.jpg": _image()}, "/p/boot")
    assert result.data == JPEG
    assert result.image_url is not None and result.image_url.endswith("/cdn/boot.jpg")
    assert result.page_title == "Navy Oxford Shirt"


def test_jsonld_product_with_image_list_takes_the_first() -> None:
    page = _html(_page(_ldjson({"@type": "Product",
                                "image": ["/cdn/first.jpg", "/cdn/second.jpg"]})))
    result = _resolve({"/p/boot": page, "/cdn/first.jpg": _image()}, "/p/boot")
    assert result.image_url is not None and result.image_url.endswith("/cdn/first.jpg")


def test_jsonld_product_with_image_object_url() -> None:
    page = _html(_page(_ldjson({"@type": "Product",
                                "image": {"@type": "ImageObject", "url": "/cdn/obj.jpg"}})))
    result = _resolve({"/p/boot": page, "/cdn/obj.jpg": _image()}, "/p/boot")
    assert result.image_url is not None and result.image_url.endswith("/cdn/obj.jpg")


def test_jsonld_product_image_by_id_resolves_through_the_graph() -> None:
    # Loake's shape: a WooCommerce @graph where Product.image is an @id
    # pointing at an ImageObject node elsewhere in the graph.
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "@id": "https://shop.example/p/boot/#webpage",
             "primaryImageOfPage": {"@id": "https://shop.example/p/boot/#primaryimage"}},
            {"@type": "ImageObject", "@id": "https://shop.example/p/boot/#primaryimage",
             "contentUrl": "/cdn/CHACHR-SIDE-min.webp"},
            {"@type": "Product", "name": "Chatsworth",
             "image": {"@id": "https://shop.example/p/boot/#primaryimage"}},
        ],
    }
    page = _html(_page(_ldjson(graph)))
    result = _resolve(
        {"/p/boot": page, "/cdn/CHACHR-SIDE-min.webp": _image(WEBP, "image/webp")}, "/p/boot"
    )
    assert result.data == WEBP
    assert result.content_type == "image/webp"
    assert result.filename == "CHACHR-SIDE-min.webp"


def test_jsonld_type_given_as_a_list() -> None:
    page = _html(_page(_ldjson({"@type": ["Product", "IndividualProduct"],
                                "image": "/cdn/l.jpg"})))
    result = _resolve({"/p/boot": page, "/cdn/l.jpg": _image()}, "/p/boot")
    assert result.image_url is not None and result.image_url.endswith("/cdn/l.jpg")


def test_jsonld_top_level_list() -> None:
    page = _html(_page(_ldjson([{"@type": "BreadcrumbList", "itemListElement": []},
                                {"@type": "Product", "image": "/cdn/t.jpg"}])))
    result = _resolve({"/p/boot": page, "/cdn/t.jpg": _image()}, "/p/boot")
    assert result.image_url is not None and result.image_url.endswith("/cdn/t.jpg")


def test_jsonld_invalid_block_is_skipped() -> None:
    head = (
        '<script type="application/ld+json">{"@type": "Product", "image": </script>'
        + _ldjson({"@type": "Product", "image": "/cdn/ok.jpg"})
    )
    result = _resolve({"/p/boot": _html(_page(head)), "/cdn/ok.jpg": _image()}, "/p/boot")
    assert result.image_url is not None and result.image_url.endswith("/cdn/ok.jpg")


def test_jsonld_without_a_product_reports_no_image() -> None:
    page = _html(_page(_ldjson({"@type": "Organization", "logo": "/cdn/logo.png"})))
    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/p/boot": page}, "/p/boot")
    assert str(exc.value) == (
        "No product image found on that page. Paste a direct image link or a file path."
    )


def test_jsonld_image_with_a_non_http_scheme_is_not_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The JSON-LD candidate goes through the same scheme check as og:image.
    async def no_lookup(host: str) -> list:
        raise AssertionError(f"looked up {host}: the scheme should be refused first")

    monkeypatch.setattr(image_source, "_resolve_addresses", no_lookup)
    page = _html(_page(_ldjson({"@type": "Product", "image": "ftp://example.com/x.jpg"})))
    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/p/boot": page}, "/p/boot")
    assert str(exc.value) == (
        "No product image found on that page. Paste a direct image link or a file path."
    )


@pytest.mark.parametrize("junk", ["[" * 100000, '{"a":' * 100000], ids=["list", "object"])
def test_jsonld_pathologically_nested_block_is_skipped(junk: str) -> None:
    head = (
        f'<script type="application/ld+json">{junk}</script>'
        + _ldjson({"@type": "Product", "image": "/cdn/ok.jpg"})
    )
    result = _resolve({"/p/boot": _html(_page(head)), "/cdn/ok.jpg": _image()}, "/p/boot")
    assert result.image_url is not None and result.image_url.endswith("/cdn/ok.jpg")


def test_jsonld_image_id_resolves_across_script_blocks() -> None:
    head = _ldjson(
        {"@type": "ImageObject", "@id": "https://shop.example/#img", "url": "/cdn/x.jpg"}
    ) + _ldjson({"@type": "Product", "image": {"@id": "https://shop.example/#img"}})
    result = _resolve({"/p/boot": _html(_page(head)), "/cdn/x.jpg": _image()}, "/p/boot")
    assert result.image_url is not None and result.image_url.endswith("/cdn/x.jpg")


def test_jsonld_image_urls_are_stripped_and_blank_ones_skipped() -> None:
    head = _ldjson({"@type": "Product", "image": "   "}) + _ldjson(
        {"@type": "Product", "image": {"url": "  /cdn/s.jpg\n"}}
    )
    result = _resolve({"/p/boot": _html(_page(head)), "/cdn/s.jpg": _image()}, "/p/boot")
    assert result.image_url is not None and result.image_url.endswith("/cdn/s.jpg")


def test_og_image_wins_over_jsonld() -> None:
    page = _html(_page(
        '<meta property="og:image" content="/cdn/og.jpg">'
        + _ldjson({"@type": "Product", "image": "/cdn/ld.jpg"})
    ))
    result = _resolve({"/p/boot": page, "/cdn/og.jpg": _image()}, "/p/boot")
    assert result.image_url is not None and result.image_url.endswith("/cdn/og.jpg")


# --- Redirects --------------------------------------------------------------


def _redirect(location: str) -> Callable:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": location})

    return handler


def test_page_redirected_to_another_path_is_refused() -> None:
    logo_page = _page_with_base('<meta property="og:image" content="{base}/cdn/logo.png">')

    async def run() -> None:
        app = web.Application()
        app.router.add_get("/p/shirt", _redirect("/collections/mens"))
        app.router.add_get("/collections/mens", logo_page)
        app.router.add_get("/cdn/logo.png", _image(PNG, "image/png"))
        server = TestServer(app)
        await server.start_server()
        try:
            base = str(server.make_url("")).rstrip("/")
            with pytest.raises(ImageSourceError) as exc:
                await resolve_image(
                    f"{base}/p/shirt", allow_local_files=False, allow_private_hosts=True
                )
            assert str(exc.value) == (
                f"That link redirected to {base}/collections/mens, which doesn't look "
                "like the product page. Paste a direct image link or a file path."
            )
        finally:
            await server.close()

    asyncio.run(run())


def test_page_redirected_only_on_case_or_trailing_slash_still_resolves() -> None:
    page = _page_with_base('<meta property="og:image" content="{base}/cdn/a.jpg">')
    result = _resolve(
        {"/p/Shirt/": _redirect("/p/shirt"), "/p/shirt": page, "/cdn/a.jpg": _image()},
        "/p/Shirt/",
    )
    assert result.data == JPEG
    assert result.page_title == "Navy Oxford Shirt"


def test_direct_image_link_redirected_to_another_path_still_resolves() -> None:
    result = _resolve(
        {"/img/shirt.jpg": _redirect("/cdn/v2/shirt.jpg"), "/cdn/v2/shirt.jpg": _image()},
        "/img/shirt.jpg",
    )
    assert result.data == JPEG
    assert result.image_url is not None and result.image_url.endswith("/cdn/v2/shirt.jpg")


def test_og_image_redirected_to_another_path_still_resolves() -> None:
    page = _page_with_base('<meta property="og:image" content="{base}/cdn/a.jpg">')
    result = _resolve(
        {
            "/p/shirt": page,
            "/cdn/a.jpg": _redirect("/cdn/resized/a.jpg"),
            "/cdn/resized/a.jpg": _image(),
        },
        "/p/shirt",
    )
    assert result.data == JPEG
    assert result.image_url is not None and result.image_url.endswith("/cdn/resized/a.jpg")


# --- Fetch guards -----------------------------------------------------------


def test_loopback_is_refused_without_the_opt_in() -> None:
    # Port 9 has nothing listening: the check must refuse before connecting.
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(resolve_image("http://127.0.0.1:9/p/shirt", allow_local_files=False))
    assert str(exc.value) == PRIVATE


def test_hostname_resolving_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(host: str) -> list:
        assert host == "shop.example"
        # One public and one private record: "any" private address refuses.
        return [ipaddress.ip_address("203.0.113.5"), ipaddress.ip_address("10.0.0.7")]

    monkeypatch.setattr(image_source, "_resolve_addresses", fake_resolve)
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(resolve_image("http://shop.example/p/shirt", allow_local_files=False))
    assert str(exc.value) == PRIVATE


def test_redirect_to_a_private_address_is_refused_mid_chain() -> None:
    # The first hop is the loopback test server (opted in); the second is not.
    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/p/shirt": _redirect("http://10.0.0.7/p/shirt")}, "/p/shirt")
    assert str(exc.value) == PRIVATE


def test_more_than_five_redirects_is_refused() -> None:
    routes = {f"/r/{n}": _redirect(f"/r/{n + 1}") for n in range(7)}
    with pytest.raises(ImageSourceError) as exc:
        _resolve(routes, "/r/0")
    assert str(exc.value) == "That link redirected too many times."


def test_five_redirects_to_an_image_still_resolve() -> None:
    routes = {f"/r/{n}": _redirect(f"/r/{n + 1}") for n in range(4)}
    routes["/r/4"] = _redirect("/img/shirt.jpg")
    routes["/img/shirt.jpg"] = _image()
    result = _resolve(routes, "/r/0")
    assert result.data == JPEG
    assert result.image_url is not None and result.image_url.endswith("/img/shirt.jpg")


def test_redirect_to_a_file_url_is_refused() -> None:
    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/p/shirt": _redirect("file:///etc/passwd")}, "/p/shirt")
    assert str(exc.value) == "That link redirected to an unsupported address."


@pytest.mark.parametrize(
    "image_url", ["ftp://example.com/x.jpg", "file://example.com/x.jpg"], ids=["ftp", "file"]
)
def test_og_image_with_a_non_http_scheme_is_not_fetched(
    image_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_lookup(host: str) -> list:
        raise AssertionError(f"looked up {host}: the scheme should be refused first")

    monkeypatch.setattr(image_source, "_resolve_addresses", no_lookup)
    page = _html(_page(f'<meta property="og:image" content="{image_url}">'))
    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/p/shirt": page}, "/p/shirt")
    assert str(exc.value) == (
        "No product image found on that page. Paste a direct image link or a file path."
    )


def test_fetch_refuses_a_non_http_first_hop_itself() -> None:
    # Backstop for callers: the scheme is checked before the session is used.
    url = "ftp://example.com/x.jpg"
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(image_source._fetch(None, url, "*/*", allow_private_hosts=False))  # type: ignore[arg-type]
    assert str(exc.value) == f"Could not download {url}: only http and https links work."


def test_redirect_on_the_image_hop_to_a_private_address_is_refused() -> None:
    page = _html(_page('<meta property="og:image" content="/cdn/a.jpg">'))
    with pytest.raises(ImageSourceError) as exc:
        _resolve(
            {"/p/shirt": page, "/cdn/a.jpg": _redirect("http://10.0.0.7/x.jpg")}, "/p/shirt"
        )
    assert str(exc.value) == PRIVATE


def test_multicast_address_is_refused() -> None:
    # Python's is_global is True for some multicast ranges; the guard refuses them anyway.
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(resolve_image("http://224.0.0.1/", allow_local_files=False))
    assert str(exc.value) == PRIVATE


def test_hostname_that_is_not_valid_idna_is_a_download_error() -> None:
    # A DNS label over 63 characters fails IDNA encoding before any lookup.
    url = f"http://{'a' * 64}.example/p/shirt"
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(resolve_image(url, allow_local_files=False))
    assert str(exc.value).startswith(f"Could not download {url}: ")


# --- Redirect path normalisation --------------------------------------------


def _reencoding_page(location: str) -> Callable:
    """Redirect once to ``location`` (another spelling of the same path), then serve the page."""

    async def handler(request: web.Request) -> web.Response:
        if "once" not in request.query:
            return web.Response(status=302, headers={"Location": location})
        return web.Response(
            text=_page('<meta property="og:image" content="/cdn/a.jpg">'),
            content_type="text/html",
        )

    return handler


@pytest.mark.parametrize(
    ("route", "requested", "location"),
    [
        ("/p/café-shirt", "/p/café-shirt", "/p/caf%C3%A9-shirt?once=1"),
        ("/p/a b", "/p/a b", "/p/a%20b?once=1"),
        ("/p/shirt", "/p/./shirt", "/p/shirt?once=1"),
        ("/p/a~b", "/p/a%7eb", "/p/a~b?once=1"),
    ],
    ids=["utf8", "space", "dot-segment", "tilde"],
)
def test_redirect_that_only_reencodes_the_path_is_not_refused(
    route: str, requested: str, location: str
) -> None:
    result = _resolve({route: _reencoding_page(location), "/cdn/a.jpg": _image()}, requested)
    assert result.data == JPEG
    assert result.page_title == "Navy Oxford Shirt"


def test_path_key_ignores_encoding_dot_segments_case_and_trailing_slash() -> None:
    key = image_source._path_key
    assert key("http://s/p/caf%C3%A9-shirt") == key("https://t/p/café-shirt/")
    assert key("http://s/p/a%20b") == key("http://s/p/a b")
    assert key("http://s/p/./shirt") == key("http://s/p/x/../Shirt")
    assert key("http://s/p/a%7eb") == key("http://s/p/a~b")
    assert key("http://s/p/shirt?v=2") == key("http://s/p/shirt")
    assert key("http://s/p/shirt") != key("http://s/collections/mens")


# --- Local files ------------------------------------------------------------


def test_local_file(tmp_path: Path) -> None:
    photo = tmp_path / "shirt.PNG"
    photo.write_bytes(PNG)
    result = asyncio.run(_resolve_local(str(photo)))
    assert result == ResolvedImage(
        data=PNG,
        content_type="image/png",
        filename="shirt.PNG",
        image_url=None,
        page_title=None,
    )


def test_missing_file(tmp_path: Path) -> None:
    missing = str(tmp_path / "nope.jpg")
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(_resolve_local(missing))
    assert str(exc.value) == f"No such file: {missing}"


def test_unsupported_extension(tmp_path: Path) -> None:
    doc = tmp_path / "receipt.pdf"
    doc.write_bytes(b"%PDF-1.4")
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(_resolve_local(str(doc)))
    assert str(exc.value) == "Unsupported file type .pdf."


def test_local_file_over_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_source, "_MAX_BYTES", 1000)
    photo = tmp_path / "big.jpg"
    photo.write_bytes(b"x" * 5000)
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(_resolve_local(str(photo)))
    assert str(exc.value).startswith("Image is ")


def test_ftp_scheme_is_treated_as_a_missing_path() -> None:
    source = "ftp://shop.example/shirt.jpg"
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(_resolve_local(source))
    assert str(exc.value) == f"No such file: {source}"


def test_local_path_is_refused_when_local_files_are_not_allowed(tmp_path: Path) -> None:
    photo = tmp_path / "shirt.jpg"
    photo.write_bytes(JPEG)
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(resolve_image(str(photo), allow_local_files=False))
    assert str(exc.value) == (
        "Local file paths only work when the server runs on your own machine. "
        "Paste a product page or image link."
    )


async def _resolve_local(source: str) -> ResolvedImage:
    return await resolve_image(source, allow_local_files=True)


# --- Final fix wave: locale redirects, address classes, caps, timeout -------


@pytest.mark.parametrize(
    ("requested", "final", "expected"),
    [
        ("/p/mens/categories/clothing/shirts/BE554",
         "/uk/p/mens/categories/clothing/shirts/BE554", True),
        ("/uk/p/mens/categories/clothing/shirts/BE554",
         "/p/mens/categories/clothing/shirts/BE554", True),
        ("/products/tee", "/en-gb/eu/products/tee", True),
        ("/p/Caf%C3%A9", "/fr/p/café/", True),
        ("/collections/mens/products/tee", "/products/tee", True),
        ("/products/mens-tree-runners", "/collections/mens", False),
        ("/p/shirt", "/", False),
        ("/p/shirt", "/uk", False),
        ("/", "/uk", False),
        ("/p/shirt", "/x/ap/shirt", False),
        ("/shirt", "/uk/tshirt", False),
        ("/p/shirt", "/q/shirt", False),
    ],
)
def test_same_page(requested: str, final: str, expected: bool) -> None:
    host = "https://shop.example"
    assert image_source._same_page(f"{host}{requested}", f"{host}{final}") is expected


def test_locale_prefix_redirect_resolves() -> None:
    # J.Crew geo-redirects /p/... to /uk/p/...: the same product page.
    page = _page_with_base('<meta property="og:image" content="{base}/cdn/be554.jpg">')
    result = _resolve(
        {
            "/p/mens/shirts/BE554": _redirect("/uk/p/mens/shirts/BE554"),
            "/uk/p/mens/shirts/BE554": page,
            "/cdn/be554.jpg": _image(),
        },
        "/p/mens/shirts/BE554",
    )
    assert result.data == JPEG
    assert result.page_title == "Navy Oxford Shirt"


def test_page_redirected_to_the_home_page_is_refused() -> None:
    logo_page = _page_with_base('<meta property="og:image" content="{base}/cdn/logo.png">')

    async def run() -> None:
        app = web.Application()
        app.router.add_get("/p/shirt", _redirect("/"))
        app.router.add_get("/", logo_page)
        app.router.add_get("/cdn/logo.png", _image(PNG, "image/png"))
        server = TestServer(app)
        await server.start_server()
        try:
            base = str(server.make_url("")).rstrip("/")
            with pytest.raises(ImageSourceError) as exc:
                await resolve_image(
                    f"{base}/p/shirt", allow_local_files=False, allow_private_hosts=True
                )
            assert str(exc.value) == (
                f"That link redirected to {base}/, which doesn't look "
                "like the product page. Paste a direct image link or a file path."
            )
        finally:
            await server.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "host",
    [
        "[::1]",
        "[::ffff:127.0.0.1]",
        "[::ffff:10.0.0.1]",
        "0.0.0.0",
        "[::]",
        "100.64.0.1",
        "169.254.169.254",
        "[fe80::1%25eth0]",
        "[fc00::1]",
        "224.0.0.1",
        # M2: IPv6 addresses that embed an IPv4 one.
        "[::127.0.0.1]",
        "[64:ff9b::7f00:1]",
        "[2002:7f00:1::]",
    ],
)
def test_address_guard_refuses_non_public_address_classes(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_lookup(name: str) -> list:
        raise AssertionError(f"looked up {name}: IP literals need no lookup")

    monkeypatch.setattr(image_source, "_resolve_addresses", no_lookup)
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(
            image_source._check_public_host(f"http://{host}/", allow_private_hosts=False)
        )
    assert str(exc.value) == PRIVATE


def test_address_guard_opt_in_exempts_only_loopback() -> None:
    asyncio.run(image_source._check_public_host("http://127.0.0.1/", allow_private_hosts=True))
    with pytest.raises(ImageSourceError) as exc:
        asyncio.run(
            image_source._check_public_host("http://10.0.0.7/", allow_private_hosts=True)
        )
    assert str(exc.value) == PRIVATE


def test_address_guard_lets_a_public_address_through() -> None:
    asyncio.run(image_source._check_public_host("http://8.8.8.8/", allow_private_hosts=False))


def test_page_title_is_capped_at_200_characters() -> None:
    page = _html(_page('<meta property="og:image" content="/cdn/a.jpg">', "T" * 500))
    result = _resolve({"/p/shirt": page, "/cdn/a.jpg": _image()}, "/p/shirt")
    assert result.page_title == "T" * 200


def test_whole_url_resolution_has_an_overall_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_source, "_OVERALL_TIMEOUT_SECONDS", 0.5)

    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(3)
        return web.Response(body=JPEG, content_type="image/jpeg")

    with pytest.raises(ImageSourceError) as exc:
        _resolve({"/img/slow.jpg": slow}, "/img/slow.jpg")
    assert str(exc.value) == "The download timed out after 0.5 seconds."
