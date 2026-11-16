"""A value HTTP cannot carry is never reported as "could not reach", and never shown.

The token is checked when the client is built (#359): a token HTTP cannot carry exactly as
given raises ``ValueError`` there, without showing it. A header passed to
``request(headers=...)`` is sent as given, so HTTP refuses it at send time. h11 refuses it
with ``LocalProtocolError`` before a byte is sent, and httpx refuses a non-ASCII one with
``UnicodeEncodeError`` while it encodes it. Until #351 the first surfaced as
``APIConnectionError("Could not reach ...")`` with the refused value in its message, and
the second escaped as an untyped error. Both now raise ``UnsendableRequestError``, which
names the base URL and nothing else. The value may be a secret: once it was the bearer
token, and a caller's own header can be an API key.

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

#: A caller's own secret, fabricated, in a header passed to ``request(headers=...)``.
CALLER_SECRET = "sk-caller-8jHdL3cQyW1nZuEa"
#: The same header with the trailing newline a value read from a file keeps (h11 refuses
#: it), and with a non-ASCII character (httpx refuses it while encoding).
NEWLINE_HEADER = {"X-Api-Key": CALLER_SECRET + "\n"}
NON_ASCII_HEADER = {"X-Api-Key": CALLER_SECRET + "é"}


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


def assert_unsendable(error, base_url, secret=CALLER_SECRET):
    """The error says what happened, names the base URL, and carries no secret."""
    assert isinstance(error, UnsendableRequestError)
    assert isinstance(error, APIConnectionError)  # what callers already catch
    assert "Could not reach" not in str(error)
    assert f"Could not send the request to {base_url}" in str(error)
    # Not chained and not even its context: a reporter walking either would find the value.
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    for surface in (str(error), repr(error), repr(vars(error)), rendered):
        assert secret not in surface
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

    def test_a_header_given_to_request(self, loopback):
        url, received = loopback
        with (
            BaseCradle(token=FAKE_TOKEN, base_url=url) as bc,
            pytest.raises(UnsendableRequestError) as caught,
        ):
            bc.request("GET", "/timelines", headers=NEWLINE_HEADER)
        assert_unsendable(caught.value, url)
        assert received == []

    async def test_a_header_given_to_request_async(self, loopback):
        url, received = loopback
        async with AsyncBaseCradle(token=FAKE_TOKEN, base_url=url) as abc:
            with pytest.raises(UnsendableRequestError) as caught:
                await abc.request("GET", "/timelines", headers=NEWLINE_HEADER)
        assert_unsendable(caught.value, url)
        assert received == []

    def test_is_never_retried(self, loopback, monkeypatch):
        """A retry would fail the same way, so even a ``GET`` with retries on is not re-sent."""
        url, received = loopback
        monkeypatch.setattr(BaseCradle, "_retry_backoff", staticmethod(never_backs_off))
        with (
            BaseCradle(token=FAKE_TOKEN, base_url=url, max_retries=3) as bc,
            pytest.raises(UnsendableRequestError),
        ):
            bc.request("GET", "/timelines", headers=NEWLINE_HEADER)
        assert received == []

    async def test_is_never_retried_async(self, loopback, monkeypatch):
        url, received = loopback
        monkeypatch.setattr(AsyncBaseCradle, "_retry_backoff", staticmethod(never_backs_off))
        async with AsyncBaseCradle(token=FAKE_TOKEN, base_url=url, max_retries=3) as abc:
            with pytest.raises(UnsendableRequestError):
                await abc.request("GET", "/timelines", headers=NEWLINE_HEADER)
        assert received == []


class TestANonAsciiHeaderValue:
    """``UnicodeEncodeError``: httpx refuses it while encoding, before anything is sent."""

    def test_sync(self, loopback):
        url, received = loopback
        with (
            BaseCradle(token=FAKE_TOKEN, base_url=url) as bc,
            pytest.raises(UnsendableRequestError) as caught,
        ):
            bc.request("GET", "/timelines", headers=NON_ASCII_HEADER)
        assert_unsendable(caught.value, url)
        assert received == []

    async def test_async(self, loopback):
        url, received = loopback
        async with AsyncBaseCradle(token=FAKE_TOKEN, base_url=url) as abc:
            with pytest.raises(UnsendableRequestError) as caught:
                await abc.request("GET", "/timelines", headers=NON_ASCII_HEADER)
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


class TestATokenHTTPCannotCarry:
    """Refused when the client is built, as ``ValueError``, never stripped (#359).

    The idempotency key's rule applied to the token: printable ASCII, with spaces or tabs
    only between characters. The message says where the fault is and shows no part of the
    token, not even the character at fault.
    """

    @pytest.mark.parametrize(
        ("token", "where"),
        [
            (FAKE_TOKEN + "\n", "index 39 is a control character"),
            (FAKE_TOKEN + "\r\n", "index 39 is a control character"),
            (FAKE_TOKEN[:20] + "\x00" + FAKE_TOKEN[20:], "index 20 is a control character"),
            (FAKE_TOKEN[:-1] + "é", "index 38 is not ASCII"),
            (FAKE_TOKEN + " ", "begins or ends with whitespace"),
            ("\t" + FAKE_TOKEN, "begins or ends with whitespace"),
            (" ", "begins or ends with whitespace"),
        ],
    )
    @pytest.mark.parametrize("cls", [BaseCradle, AsyncBaseCradle])
    def test_is_refused_without_showing_it(self, cls, token, where):
        with pytest.raises(ValueError, match="token cannot be sent in an HTTP header") as caught:
            cls(token=token)
        error = caught.value
        assert where in str(error)
        assert error.__context__ is None
        for surface in (str(error), repr(error), repr(vars(error))):
            assert FAKE_TOKEN[:12] not in surface
            assert FAKE_TOKEN[-12:] not in surface
            assert "é" not in surface  # not even the one character at fault

    @pytest.mark.parametrize("cls", [BaseCradle, AsyncBaseCradle])
    def test_from_the_environment_too(self, cls, monkeypatch):
        """The usual way a token arrives with its newline: an env file that kept it."""
        monkeypatch.setenv("BASECRADLE_TOKEN", FAKE_TOKEN + "\n")
        with pytest.raises(ValueError, match="index 39 is a control character") as caught:
            cls()
        assert "trailing newline" in str(caught.value)

    @pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
    def test_a_kept_newline_is_named(self, ending):
        """Unix and Windows line endings alike."""
        with pytest.raises(ValueError, match="keeps its trailing newline"):
            BaseCradle(token=FAKE_TOKEN + ending)

    @pytest.mark.parametrize(
        "token",
        [FAKE_TOKEN[:20] + "\x00" + FAKE_TOKEN[20:], "\t" + FAKE_TOKEN, FAKE_TOKEN + " "],
    )
    def test_no_newline_is_suggested_where_there_is_none(self, token):
        with pytest.raises(ValueError) as caught:
            BaseCradle(token=token)
        assert "trailing newline" not in str(caught.value)
        assert "Check where the token was read from" in str(caught.value)

    @pytest.mark.parametrize("cls", [BaseCradle, AsyncBaseCradle])
    def test_a_token_http_can_carry_is_accepted_as_given(self, cls):
        """Never stripped: the client keeps exactly what it was given."""
        assert cls(token=FAKE_TOKEN).token == FAKE_TOKEN


def h11_refusal():
    """What h11 says when it refuses a header: the whole value. The worst case stands in
    here, a refusal holding the bearer token, so the pin covers any secret it could hold."""
    return httpx.LocalProtocolError(f"Illegal header value b'Bearer {FAKE_TOKEN}\\n'")


class TestLogin:
    def test_sync(self, api):
        api.post("/session").mock(side_effect=h11_refusal())
        with pytest.raises(UnsendableRequestError) as caught:
            BaseCradle.login(email_address="john@example.com", password="correct horse")
        assert_unsendable(caught.value, BASE_URL, secret=FAKE_TOKEN)

    async def test_async(self, api):
        api.post("/session").mock(side_effect=h11_refusal())
        with pytest.raises(UnsendableRequestError) as caught:
            await AsyncBaseCradle.login(email_address="john@example.com", password="correct horse")
        assert_unsendable(caught.value, BASE_URL, secret=FAKE_TOKEN)
