"""Turn an ``add_item`` source into image bytes.

A source is a retailer product-page link, a direct image link, or a local
file path. Product pages are followed to their ``og:image`` (falling back to
``twitter:image``, then a JSON-LD ``Product`` image), at most one hop. Nothing here knows about Wardrowbe's
API: the caller uploads the returned bytes.

Shop fetches go through ``curl_cffi`` impersonating Chrome. Cloudflare-fronted
shops (Loake, J.Crew) answer plain Python HTTP clients with 403 by their TLS
fingerprint, whatever headers they send. Every hop is checked against private
and local addresses before it is fetched, and redirects are followed by hand
so each hop gets the same check.

Every failure raises ``ImageSourceError`` whose message is shown to the user
verbatim, so the wording is plain English.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import posixpath
import socket
import warnings
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests import exceptions as curl_errors
from curl_cffi.utils import CurlCffiWarning

_LOGGER = logging.getLogger(__name__)

_MAX_BYTES = 10 * 1024 * 1024
_TIMEOUT_SECONDS = 20
# Ceiling on the whole URL resolution: every redirect hop, the page and the
# image. Each request also has its own _TIMEOUT_SECONDS.
_OVERALL_TIMEOUT_SECONDS = 60
_MAX_TITLE_CHARS = 200
_MAX_REDIRECTS = 5

# The impersonation supplies Chrome's own User-Agent and header set; only
# Accept is overridden, so image CDNs don't negotiate AVIF, which Wardrowbe
# can't store.
_IMPERSONATE = "chrome"
_ACCEPT_IMAGE = "image/jpeg,image/png,image/webp;q=0.9,*/*;q=0.5"
_ACCEPT_PAGE = "text/html"
# The first request may be a page or an image, so it asks for either.
_ACCEPT_FIRST = f"{_ACCEPT_PAGE},{_ACCEPT_IMAGE}"

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_IMAGE_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
)
_EXTENSION_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
_BLOCKED_STATUSES = frozenset({401, 403, 429})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_IMAGE_META_KEYS = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image")

_WEB_SCHEMES = ("http", "https")
# Defence in depth under the scheme checks below: the bundled libcurl would
# otherwise read file:// and speak ftp, gopher, dict and the rest.
_CURL_OPTIONS = {
    CurlOpt.PROTOCOLS_STR: b"http,https",
    CurlOpt.REDIR_PROTOCOLS_STR: b"http,https",
}

_NO_IMAGE_MESSAGE = (
    "No product image found on that page. Paste a direct image link or a file path."
)
_PRIVATE_ADDRESS_MESSAGE = (
    "That link points to a private or local network address, which I won't fetch."
)
_LOCAL_FILES_MESSAGE = (
    "Local file paths only work when the server runs on your own machine. "
    "Paste a product page or image link."
)


@dataclass(frozen=True)
class ResolvedImage:
    """Image bytes ready to upload, plus where they came from."""

    data: bytes
    content_type: str  # image/jpeg | image/png | image/webp | image/heic | image/heif
    filename: str
    image_url: str | None  # the URL actually downloaded (None for local files)
    page_title: str | None  # og:title or <title> when the source was a page


class ImageSourceError(Exception):
    """The source could not be turned into an image. Message is user-facing."""


async def resolve_image(
    source: str,
    *,
    allow_local_files: bool,
    allow_private_hosts: bool = False,
) -> ResolvedImage:
    """Fetch or read the image ``source`` points at.

    http/https sources are downloaded (following one page → image hop).
    Anything else is treated as a local file path, which is only allowed
    when ``allow_local_files`` is set: the stdio transport, where the caller
    and this server share a machine.

    ``allow_private_hosts`` lets loopback addresses through the public-address
    guard so tests can serve from 127.0.0.1. The server never sets it.
    """
    source = source.strip()
    if _is_web_url(source):
        try:
            async with asyncio.timeout(_OVERALL_TIMEOUT_SECONDS):
                return await _resolve_url(source, allow_private_hosts=allow_private_hosts)
        except TimeoutError as err:
            raise ImageSourceError(_timed_out(_OVERALL_TIMEOUT_SECONDS)) from err
    if not allow_local_files:
        raise ImageSourceError(_LOCAL_FILES_MESSAGE)
    return _resolve_path(source)


# --- URLs -----------------------------------------------------------------


async def _resolve_url(url: str, *, allow_private_hosts: bool) -> ResolvedImage:
    async with AsyncSession(
        impersonate=_IMPERSONATE, timeout=_TIMEOUT_SECONDS, curl_options=_CURL_OPTIONS
    ) as session:
        _start_quietly(session)
        final_url, content_type, body = await _fetch(
            session, url, _ACCEPT_FIRST, allow_private_hosts=allow_private_hosts
        )
        if content_type not in _HTML_TYPES:
            return _as_image(final_url, content_type, body, page_title=None)

        # A product link that redirects to a different page (a category, the
        # home page) would hand us that page's og:image, usually the site logo.
        # A redirect that only adds or drops whole leading segments, such as a
        # locale prefix (/uk, /en-gb/eu), keeps the product slug and is the
        # same page; see _same_page.
        if not _same_page(url, final_url):
            raise ImageSourceError(
                f"That link redirected to {final_url}, which doesn't look like the "
                "product page. Paste a direct image link or a file path."
            )

        meta = _parse_page(body)
        image_url = meta.image_url()
        if image_url is None:
            raise ImageSourceError(_NO_IMAGE_MESSAGE)
        image_url = urljoin(final_url, image_url)
        # An ftp:, file: or data: "image" is no usable product image.
        if not _is_web_url(image_url):
            raise ImageSourceError(_NO_IMAGE_MESSAGE)
        _LOGGER.debug("Page %s points at image %s", final_url, image_url)

        final_image_url, image_type, image_body = await _fetch(
            session, image_url, _ACCEPT_IMAGE, allow_private_hosts=allow_private_hosts
        )
        return _as_image(final_image_url, image_type, image_body, page_title=meta.title())


def _is_web_url(url: str) -> bool:
    return urlsplit(url).scheme.lower() in _WEB_SCHEMES


def _start_quietly(session: AsyncSession) -> None:
    """Create the session's curl handle now, without the Windows warning.

    On Windows, asyncio's default Proactor loop lacks ``add_reader``, so
    curl_cffi runs its own selector thread and warns, once per loop, that it
    did. The fallback works; the warning is only noise on stderr. The handle
    would be created lazily on the first request anyway. Nothing is awaited
    inside ``catch_warnings``, so no other coroutine sees the filter.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=r"\s*Proactor event loop", category=CurlCffiWarning
        )
        session.acurl  # noqa: B018 - the property creates the handle


def _path_key(url: str) -> str:
    """URL path normalised so two spellings of one path compare equal.

    Percent-decoded, dot segments removed, trailing slash dropped, lowercased.
    Scheme, host and query don't count. Both sides of the redirect check go
    through here, so a redirect that merely re-encodes the path (``café``,
    a space, ``./``, ``%7e``) is not mistaken for a different page.
    """
    path = unquote(urlsplit(url).path) or "/"
    return posixpath.normpath(path).rstrip("/").lower()


def _path_segments(url: str) -> list[str]:
    return [s for s in _path_key(url).split("/") if s]


def _same_page(requested: str, final: str) -> bool:
    """Same normalised path, or the same path with whole leading segments
    added or removed (a locale prefix such as /uk or /en-gb/eu). The shorter
    path must keep at least one segment, so the product slug survives."""
    if _path_key(requested) == _path_key(final):
        return True
    short, long_ = sorted((_path_segments(requested), _path_segments(final)), key=len)
    return bool(short) and len(long_) > len(short) and long_[-len(short):] == short


async def _fetch(
    session: AsyncSession, url: str, accept: str, *, allow_private_hosts: bool
) -> tuple[str, str, bytes]:
    """GET ``url``, following at most 5 redirects by hand.

    Returns (final URL, bare content type, body within the limit). Every hop,
    the first included, is checked for an http(s) scheme and a public host
    before it is fetched, so a public page can't bounce the request onto the
    LAN or into another protocol. Callers vet the first URL's scheme with
    their own message; the check here is the backstop.
    """
    current = url
    for hop in range(_MAX_REDIRECTS + 1):
        if not _is_web_url(current):
            if hop == 0:
                raise ImageSourceError(
                    f"Could not download {current}: only http and https links work."
                )
            raise ImageSourceError("That link redirected to an unsupported address.")
        await _check_public_host(current, allow_private_hosts=allow_private_hosts)
        location, content_type, body = await _get(session, current, accept)
        if location is None:
            return current, content_type, body
        current = urljoin(current, location)
        _LOGGER.debug("Redirected to %s", current)
    raise ImageSourceError("That link redirected too many times.")


async def _get(session: AsyncSession, url: str, accept: str) -> tuple[str | None, str, bytes]:
    """One GET without following redirects.

    Returns ``(location, content_type, body)``. For a redirect, ``location``
    is the target and the other two are empty; otherwise ``location`` is
    ``None`` and the body is within the size limit.
    """
    resp = None
    try:
        resp = await session.get(
            url, headers={"Accept": accept}, stream=True, allow_redirects=False
        )
        location = resp.headers.get("location")
        if resp.status_code in _REDIRECT_STATUSES and location:
            return location, "", b""
        if resp.status_code in _BLOCKED_STATUSES:
            raise ImageSourceError(
                f"The shop blocked the download (HTTP {resp.status_code}). "
                "Save the image from your browser and give me the file path."
            )
        if resp.status_code >= 400:
            raise ImageSourceError(f"The link returned HTTP {resp.status_code}.")
        declared = resp.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > _MAX_BYTES:
            raise ImageSourceError(_too_big(int(declared)))
        body = bytearray()
        async for chunk in resp.aiter_content():
            body.extend(chunk)
            if len(body) > _MAX_BYTES:
                raise ImageSourceError(_too_big(len(body)))
        content_type = (
            (resp.headers.get("content-type") or "application/octet-stream")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        return None, content_type, bytes(body)
    except curl_errors.Timeout as err:
        raise ImageSourceError(_timed_out(_TIMEOUT_SECONDS)) from err
    except curl_errors.RequestException as err:
        raise ImageSourceError(f"Could not download {url}: {err}") from err
    finally:
        if resp is not None:
            await resp.aclose()


async def _check_public_host(url: str, *, allow_private_hosts: bool) -> None:
    """Refuse ``url`` unless every address its host resolves to is public.

    IP-literal hosts are checked directly; names go through ``getaddrinfo``
    and every record must pass. ``allow_private_hosts`` exempts loopback
    only, for tests that serve from 127.0.0.1; private, link-local, CGNAT,
    multicast, reserved and unspecified addresses are refused regardless.
    Python's ``is_global`` is True for multicast, hence the explicit check.
    IPv6 addresses that embed an IPv4 one are checked on both; see
    ``_is_refused``.

    Known gap (spec decision 7): curl resolves the name again when it
    connects, so a record that changes in between (DNS rebinding) is not
    caught.
    """
    host = urlsplit(url).hostname
    if not host:
        raise ImageSourceError(f"Could not download {url}: the link has no host.")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = await _resolve_addresses(host)
        # UnicodeError: the name fails IDNA encoding (bad characters, or a
        # label over 63 characters) before any lookup happens.
        except (socket.gaierror, UnicodeError) as err:
            raise ImageSourceError(f"Could not download {url}: {err}") from err
    for address in addresses:
        if _is_refused(address, allow_loopback=allow_private_hosts):
            raise ImageSourceError(_PRIVATE_ADDRESS_MESSAGE)


_Address = ipaddress.IPv4Address | ipaddress.IPv6Address
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def _is_refused(address: _Address, *, allow_loopback: bool) -> bool:
    """True unless ``address`` is public (or loopback, when allowed).

    An IPv6 address that carries an IPv4 one (mapped, 6to4, Teredo, NAT64)
    is refused when the embedded address is not public either, since the
    packets may end up there. IPv4-compatible addresses (``::/96``) are
    deprecated and refused outright.
    """
    if allow_loopback and address.is_loopback:
        return False
    if address.is_multicast or not address.is_global:
        return True
    if isinstance(address, ipaddress.IPv6Address):
        if address in _IPV4_COMPATIBLE:
            return True
        return any(
            _is_refused(embedded, allow_loopback=allow_loopback)
            for embedded in _embedded_ipv4(address)
        )
    return False


def _embedded_ipv4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    embedded: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        embedded.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        embedded.extend(address.teredo)
    if address in _NAT64:
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return embedded


async def _resolve_addresses(
    host: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address ``host`` resolves to. Tests patch this."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    # IPv6 results may carry a scope suffix ("fe80::1%3") that ip_address rejects.
    return [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]


def _as_image(
    url: str, content_type: str, body: bytes, *, page_title: str | None
) -> ResolvedImage:
    if content_type not in _IMAGE_TYPES:
        raise ImageSourceError(
            f"That link returned {content_type}, not an image. "
            "Wardrowbe accepts JPEG, PNG, WebP or HEIC."
        )
    return ResolvedImage(
        data=body,
        content_type=content_type,
        filename=_filename_from_url(url, content_type),
        image_url=url,
        page_title=page_title,
    )


def _filename_from_url(url: str, content_type: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    if Path(name).suffix.lower() in _EXTENSION_TYPES:
        return name
    return f"{name or 'image'}{_TYPE_EXTENSIONS[content_type]}"


def _timed_out(seconds: float) -> str:
    return f"The download timed out after {seconds:g} seconds."


def _too_big(size: int) -> str:
    return (
        f"Image is {size / (1024 * 1024):.1f} MB; "
        f"Wardrowbe's limit is {_MAX_BYTES / (1024 * 1024):g} MB."
    )


class _PageMeta(HTMLParser):
    """Collect ``<meta>`` name/property → content pairs, the ``<title>``,
    and the raw text of every ``<script type="application/ld+json">``."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.ldjson: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._seen_title = False
        self._ldjson_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and not self._seen_title:
            self._in_title = True
            return
        values = {key: value for key, value in attrs if value is not None}
        if tag == "script":
            if (values.get("type") or "").strip().lower() == "application/ld+json":
                self._ldjson_parts = []
            return
        if tag != "meta":
            return
        key = (values.get("property") or values.get("name") or "").strip().lower()
        content = (values.get("content") or "").strip()
        if key and content:
            self.meta.setdefault(key, content)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self._seen_title = True
        if tag == "script" and self._ldjson_parts is not None:
            self.ldjson.append("".join(self._ldjson_parts))
            self._ldjson_parts = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._ldjson_parts is not None:
            self._ldjson_parts.append(data)

    def image_url(self) -> str | None:
        """The meta-tag image first, then the first JSON-LD Product image."""
        for key in _IMAGE_META_KEYS:
            if key in self.meta:
                return self.meta[key]
        return _jsonld_product_image(self.ldjson)

    def title(self) -> str | None:
        title = self.meta.get("og:title") or " ".join("".join(self._title_parts).split())
        return title[:_MAX_TITLE_CHARS] or None


def _parse_page(body: bytes) -> _PageMeta:
    parser = _PageMeta()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return parser


def _jsonld_product_image(blocks: list[str]) -> str | None:
    """The first ``Product`` image across a page's ``application/ld+json`` blocks.

    Handles a bare object, a list, and ``@graph``. ``image`` may be a string,
    a list (first element), an object with ``url``/``contentUrl``, or an
    ``@id`` pointing at another node on the page, in the same block or
    another one (WooCommerce's shape, where the ``ImageObject`` lives in the
    ``@graph``). Invalid or pathologically nested JSON is skipped silently.
    """
    nodes: list[dict[str, Any]] = []
    for block in blocks:
        try:
            nodes.extend(_jsonld_nodes(json.loads(block)))
        except (ValueError, RecursionError):
            continue
    by_id = {node["@id"]: node for node in nodes if isinstance(node.get("@id"), str)}
    for node in nodes:
        if _jsonld_has_type(node, "Product") and "image" in node:
            try:
                url = _jsonld_image_url(node["image"], by_id)
            except RecursionError:  # absurdly nested image lists
                continue
            if url is not None:
                return url
    return None


def _jsonld_nodes(data: Any) -> list[dict[str, Any]]:
    """Flatten a JSON-LD document into its object nodes (one level of @graph)."""
    nodes: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else [data]:
        if not isinstance(item, dict):
            continue
        nodes.append(item)
        graph = item.get("@graph")
        if isinstance(graph, list):
            nodes.extend(node for node in graph if isinstance(node, dict))
    return nodes


def _jsonld_has_type(node: dict[str, Any], type_name: str) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return type_name in node_type
    return node_type == type_name


def _jsonld_image_url(image: Any, by_id: dict[str, dict[str, Any]]) -> str | None:
    if isinstance(image, list):
        return _jsonld_image_url(image[0], by_id) if image else None
    if isinstance(image, str):
        return image.strip() or None
    if isinstance(image, dict):
        for key in ("url", "contentUrl"):
            value = image.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        ref = image.get("@id")
        if isinstance(ref, str) and ref in by_id:
            # Follow one @id hop only, so a self-referencing graph can't loop.
            return _jsonld_image_url(by_id[ref], {})
    return None


# --- Local files ------------------------------------------------------------


def _resolve_path(source: str) -> ResolvedImage:
    path = Path(source).expanduser()
    if not path.is_file():
        raise ImageSourceError(f"No such file: {source}")
    extension = path.suffix.lower()
    content_type = _EXTENSION_TYPES.get(extension)
    if content_type is None:
        raise ImageSourceError(f"Unsupported file type {extension or '(none)'}.")
    size = path.stat().st_size
    if size > _MAX_BYTES:
        raise ImageSourceError(_too_big(size))
    return ResolvedImage(
        data=path.read_bytes(),
        content_type=content_type,
        filename=path.name,
        image_url=None,
        page_title=None,
    )
