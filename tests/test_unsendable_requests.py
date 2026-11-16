"""A request the HTTP client refuses to send is not "could not reach" (#351).

h11 refuses a header value HTTP cannot carry, raising ``LocalProtocolError`` before a byte
is sent. httpx refuses a non-ASCII header value while it encodes it, raising
``UnicodeEncodeError``. Until #351 the first surfaced as ``APIConnectionError("Could not
reach ...")``, with the refused value in its message, and that value may be the bearer
token. The second escaped as an untyped error. Both now raise ``UnsendableRequestError``,
which names the base URL and nothing else.

respx mocks the transport *above* h11, so it can never see this class of failure. The
send-path tests here go through a real loopback socket: a server on ``127.0.0.1`` that
this module starts, counts the requests it receives, and stops. They never touch the
network. ``login()`` is not reachable this way, because its headers are fixed and a bad
``base_url`` fails as a URL error first. So its mapping is pinned with respx raising the
error h11 would raise.
"""

import http.server
import threading
import traceback

import httpx
import pytest

from basecradle import (
    APIConnectionError,
    AsyncBaseCradle,
    BaseCradle,
    UnsendableRequestError,
)
from tests.conftest import BASE_URL, FAKE_TOKEN

pytestmark = pytest.mark.anyio

#: A token read from a file or an environment variable along with its trailing newline.
NEWLINE_TOKEN = FAKE_TOKEN + "\n"


@pytest.fixture
def loopback():
    """A local HTTP server that answers 200 and counts what reaches it. Stopped after."""
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def never_backs_off(attempt):
    """Stands in for the retry backoff, which only a retry reaches."""
    pytest.fail("an unsendable GET was retried")


def assert_unsendable(error, base_url):
    """The error says what happened, names the base URL, and carries no credential."""
    assert isinstance(error, UnsendableRequestError)
    assert isinstance(error, APIConnectionError)  # what callers already catch
    assert "Could not reach" not in str(error)
    assert f"Could not send the request to {base_url}" in str(error)
    # Not chained and not even its context: a reporter walking either would find the token.
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    for surface in (str(error), repr(error), repr(vars(error)), rendered):
        assert FAKE_TOKEN not in surface


class TestTheLoopbackServesAGoodRequest:
    """The control: the same server, client and path succeed when the values are legal."""

    def test_sync(self, loopback):
        url, received = loopback
        with BaseCradle(token=FAKE_TOKEN, base_url=url) as bc:
            assert bc.request("GET", "/timelines") == {}
        assert received == ["/timelines"]

    async def test_async(self, loopback):
        url, received = loopback
        async with AsyncBaseCradle(token=FAKE_TOKEN, base_url=url) as abc:
            assert await abc.request("GET", "/timelines") == {}
        assert received == ["/timelines"]


class TestARefusedHeaderValue:
    """``LocalProtocolError``: h11 refuses the value before anything is sent."""

    def test_a_token_with_a_trailing_newline(self, loopback):
        url, received = loopback
        with (
            BaseCradle(token=NEWLINE_TOKEN, base_url=url) as bc,
            pytest.raises(UnsendableRequestError) as caught,
        ):
            bc.request("GET", "/timelines")
        assert_unsendable(caught.value, url)
        assert received == []

    async def test_a_token_with_a_trailing_newline_async(self, loopback):
        url, received = loopback
        async with AsyncBaseCradle(token=NEWLINE_TOKEN, base_url=url) as abc:
            with pytest.raises(UnsendableRequestError) as caught:
                await abc.request("GET", "/timelines")
        assert_unsendable(caught.value, url)
        assert received == []

    def test_a_header_given_to_request(self, loopback):
        url, received = loopback
        with (
            BaseCradle(token=FAKE_TOKEN, base_url=url) as bc,
            pytest.raises(UnsendableRequestError) as caught,
        ):
            bc.request("GET", "/timelines", headers={"Idempotency-Key": " key-1 "})
        assert_unsendable(caught.value, url)
        assert received == []

    async def test_a_header_given_to_request_async(self, loopback):
        url, received = loopback
        async with AsyncBaseCradle(token=FAKE_TOKEN, base_url=url) as abc:
            with pytest.raises(UnsendableRequestError) as caught:
                await abc.request("GET", "/timelines", headers={"Idempotency-Key": " key-1 "})
        assert_unsendable(caught.value, url)
        assert received == []

    def test_is_never_retried(self, loopback, monkeypatch):
        """A retry would fail the same way, so even a ``GET`` with retries on is not re-sent."""
        url, received = loopback
        monkeypatch.setattr(BaseCradle, "_retry_backoff", staticmethod(never_backs_off))
        with (
            BaseCradle(token=NEWLINE_TOKEN, base_url=url, max_retries=3) as bc,
            pytest.raises(UnsendableRequestError),
        ):
            bc.request("GET", "/timelines")
        assert received == []

    async def test_is_never_retried_async(self, loopback, monkeypatch):
        url, received = loopback
        monkeypatch.setattr(AsyncBaseCradle, "_retry_backoff", staticmethod(never_backs_off))
        async with AsyncBaseCradle(token=NEWLINE_TOKEN, base_url=url, max_retries=3) as abc:
            with pytest.raises(UnsendableRequestError):
                await abc.request("GET", "/timelines")
        assert received == []


class TestANonAsciiHeaderValue:
    """``UnicodeEncodeError``: httpx refuses it while encoding, before anything is sent."""

    def test_sync(self, loopback):
        url, received = loopback
        with (
            BaseCradle(token=FAKE_TOKEN, base_url=url) as bc,
            pytest.raises(UnsendableRequestError) as caught,
        ):
            bc.request("GET", "/timelines", headers={"Idempotency-Key": "clé-1"})
        assert_unsendable(caught.value, url)
        assert received == []

    async def test_async(self, loopback):
        url, received = loopback
        async with AsyncBaseCradle(token=FAKE_TOKEN, base_url=url) as abc:
            with pytest.raises(UnsendableRequestError) as caught:
                await abc.request("GET", "/timelines", headers={"Idempotency-Key": "clé-1"})
        assert_unsendable(caught.value, url)
        assert received == []


class TestABodyThatCannotBeEncoded:
    """A body or query fails to encode in UTF-8, not ASCII: the caller's own data, so its
    ``UnicodeEncodeError`` reaches them unchanged, never blamed on a header or the token."""

    def test_sync(self, loopback):
        url, received = loopback
        with BaseCradle(token=FAKE_TOKEN, base_url=url) as bc, pytest.raises(UnicodeEncodeError):
            bc.request("GET", "/timelines", params={"q": "\ud800"})
        assert received == []

    async def test_async(self, loopback):
        url, received = loopback
        async with AsyncBaseCradle(token=FAKE_TOKEN, base_url=url) as abc:
            with pytest.raises(UnicodeEncodeError):
                await abc.request("GET", "/timelines", params={"q": "\ud800"})
        assert received == []


class TestANonAsciiToken:
    """It fails when the client is built, as before, but no longer shows the token."""

    @pytest.mark.parametrize("cls", [BaseCradle, AsyncBaseCradle])
    def test_raises_value_error_without_the_token(self, cls):
        token = FAKE_TOKEN[:-1] + "é"
        with pytest.raises(ValueError, match="token is not ASCII") as caught:
            cls(token=token)
        error = caught.value
        assert error.__context__ is None
        for surface in (str(error), repr(error), repr(vars(error))):
            assert token not in surface
            assert FAKE_TOKEN[:-1] not in surface


def h11_refusal():
    """What h11 says when it refuses the bearer token: the whole value, token included."""
    return httpx.LocalProtocolError(f"Illegal header value b'Bearer {FAKE_TOKEN}\\n'")


class TestLogin:
    def test_sync(self, api):
        api.post("/session").mock(side_effect=h11_refusal())
        with pytest.raises(UnsendableRequestError) as caught:
            BaseCradle.login(email_address="john@example.com", password="correct horse")
        assert_unsendable(caught.value, BASE_URL)

    async def test_async(self, api):
        api.post("/session").mock(side_effect=h11_refusal())
        with pytest.raises(UnsendableRequestError) as caught:
            await AsyncBaseCradle.login(email_address="john@example.com", password="correct horse")
        assert_unsendable(caught.value, BASE_URL)
