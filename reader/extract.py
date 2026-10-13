"""Turn pasted text or a web page into clean text that is pleasant to hear."""

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
import trafilatura


class ExtractionError(Exception):
    """Something the user can fix or should know about; the message is shown on the page."""


class UnsafeURL(ExtractionError):
    """The link points somewhere the server must not fetch."""


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_LINE_MARKER = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]+|[-*+•][ \t]+|>[ \t]?)", re.MULTILINE)
_EMPHASIS = re.compile(r"[*`~]")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_END_PUNCTUATION = ".!?…:;\"'”’)"


def clean_text(text: str) -> str:
    """Strip markup and links, join wrapped lines, and make every paragraph end in punctuation.

    Paragraphs are separated by a blank line in the result. Returns "" if nothing is readable.
    """
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _LINE_MARKER.sub("", text)
    text = _EMPHASIS.sub("", text)

    paragraphs: list[str] = []
    for block in _PARAGRAPH_BREAK.split(text):
        block = " ".join(block.split())
        if not any(character.isalnum() for character in block):
            continue
        if block[-1] not in _END_PUNCTUATION:
            block += "."
        paragraphs.append(block)
    return "\n\n".join(paragraphs)


Resolver = Callable[..., list[Any]]

MAX_PAGE_BYTES = 5_000_000
MAX_REDIRECTS = 5
_USER_AGENT = "Mozilla/5.0 (compatible; pocket-tts-reader)"
# Carrier-grade NAT space: this is where Tailscale addresses live. `is_private` does not cover it.
_BLOCKED_NETWORKS = [ipaddress.ip_network("100.64.0.0/10")]


def _is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or any(address in network for network in _BLOCKED_NETWORKS)
    )


def check_url(url: str, resolve: Resolver = socket.getaddrinfo) -> None:
    """Raise unless the URL is http(s) and every address its host resolves to is public.

    Known limit: the host is resolved again when the request is made, so a hostile DNS server
    could answer differently the second time (DNS rebinding). Acceptable for one user on a tailnet.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL("Only http and https links can be read.")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = resolve(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ExtractionError(f"Could not find the website {parts.hostname}.") from error
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if _is_blocked(address):
            raise UnsafeURL(
                "That link points to a private or internal address, so it was not read."
            )


def fetch_page(
    url: str,
    http_get: Callable[..., requests.Response] = requests.get,
    resolve: Resolver = socket.getaddrinfo,
) -> bytes:
    """Download a web page, checking every hop of a redirect chain and capping the size."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, resolve)
        try:
            response = http_get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=(5, 15),
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as error:
            raise ExtractionError("Could not download that page.") from error
        with response:
            location = response.headers.get("Location")
            if 300 <= response.status_code < 400 and location:
                url = urljoin(url, location)
                continue
            if response.status_code >= 400:
                raise ExtractionError(f"The website answered with error {response.status_code}.")
            content_type = response.headers.get("Content-Type", "")
            if content_type and "html" not in content_type.lower():
                raise ExtractionError(f"That link is not a web page ({content_type}).")
            body = bytearray()
            for piece in response.iter_content(65536):
                body.extend(piece)
                if len(body) > MAX_PAGE_BYTES:
                    raise ExtractionError("That page is too large to read.")
            return bytes(body)
    raise ExtractionError("That link redirects too many times.")


MIN_ARTICLE_CHARS = 200


@dataclass(frozen=True)
class Article:
    title: str
    text: str


def extract_article(page: bytes) -> Article:
    """Pull the main article out of a web page, without menus, comments, or tables."""
    extracted = trafilatura.extract(
        page,
        include_comments=False,
        include_tables=False,
        include_links=False,
        favor_precision=True,
    )
    # trafilatura puts one paragraph per line; clean_text wants blank lines between paragraphs.
    text = clean_text((extracted or "").replace("\n", "\n\n"))
    if len(text) < MIN_ARTICLE_CHARS:
        raise ExtractionError(
            "I couldn't find an article on that page. It may need JavaScript or a login."
        )
    metadata = trafilatura.extract_metadata(page)
    title = (metadata.title if metadata is not None else None) or text.split("\n\n")[0]
    return Article(title=title.strip(), text=text)
