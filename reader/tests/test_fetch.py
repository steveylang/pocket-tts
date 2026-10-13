import ipaddress
import socket
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests
from typing_extensions import Self

from reader.extract import ExtractionError, UnsafeURL, check_url, fetch_page

HOSTS = {
    "example.com": "93.184.216.34",
    "localhost": "127.0.0.1",
    "internal.example.com": "10.0.0.5",
    "sneaky.example.com": "127.0.0.1",
    "tailnet.example.com": "100.75.66.122",
}


def resolve(host: str, port: int, type: int = 0) -> list[Any]:
    """Fake DNS: literal IPs resolve to themselves, known names to HOSTS, others fail."""
    try:
        address = str(ipaddress.ip_address(host))
    except ValueError:
        if host not in HOSTS:
            raise socket.gaierror("unknown host") from None
        address = HOSTS[host]
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, port))]


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"<html>ok</html>",
    ):
        self.status_code = status
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]


def getter(*responses: FakeResponse) -> Callable[..., FakeResponse]:
    queue = list(responses)

    def http_get(url: str, **kwargs: object) -> FakeResponse:
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        return queue.pop(0)

    return http_get


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.1.2.3/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.75.66.122/",
        "http://0.0.0.0/",
        "http://internal.example.com/",
        "http://sneaky.example.com/",
        "http://tailnet.example.com/",
    ],
)
def test_unsafe_urls_are_refused(url: str):
    with pytest.raises(UnsafeURL):
        check_url(url, resolve)


def test_localhost_is_refused_with_the_real_resolver():
    with pytest.raises(UnsafeURL):
        check_url("http://localhost:8765/")


def test_public_url_is_allowed():
    check_url("https://example.com/article", resolve)


def test_unknown_host_is_a_clear_error():
    with pytest.raises(ExtractionError, match="find"):
        check_url("https://nope.invalid/", resolve)


def test_page_bytes_are_returned():
    page = fetch_page("https://example.com/", getter(FakeResponse(body=b"<p>hi</p>")), resolve)
    assert page == b"<p>hi</p>"


def test_redirect_to_an_internal_address_is_refused():
    redirect = FakeResponse(302, {"Location": "http://internal.example.com/admin"}, b"")
    with pytest.raises(UnsafeURL):
        fetch_page("https://example.com/", getter(redirect), resolve)


def test_relative_redirects_are_followed():
    redirect = FakeResponse(301, {"Location": "/final"}, b"")
    page = fetch_page(
        "https://example.com/a", getter(redirect, FakeResponse(body=b"done")), resolve
    )
    assert page == b"done"


def test_redirect_loops_stop():
    loop = [FakeResponse(302, {"Location": "https://example.com/"}, b"") for _ in range(10)]
    with pytest.raises(ExtractionError, match="redirect"):
        fetch_page("https://example.com/", getter(*loop), resolve)


@pytest.mark.parametrize("content_type", ["application/pdf", "image/png", "application/zip"])
def test_non_html_content_is_refused(content_type: str):
    response = FakeResponse(headers={"Content-Type": content_type})
    with pytest.raises(ExtractionError, match="not a web page"):
        fetch_page("https://example.com/f", getter(response), resolve)


def test_oversized_pages_are_refused():
    response = FakeResponse(body=b"x" * 5_000_001)
    with pytest.raises(ExtractionError, match="too large"):
        fetch_page("https://example.com/big", getter(response), resolve)


def test_http_errors_are_reported():
    with pytest.raises(ExtractionError, match="404"):
        fetch_page("https://example.com/missing", getter(FakeResponse(404)), resolve)


def test_network_failures_are_reported():
    def failing(url: str, **kwargs: object) -> FakeResponse:
        raise requests.ConnectionError("down")

    with pytest.raises(ExtractionError, match="download"):
        fetch_page("https://example.com/", failing, resolve)
