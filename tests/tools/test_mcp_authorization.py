"""Public MCP authorization entry point (``start_mcp_authorization`` / ``finish_mcp_authorization``):
a host relaying the browser redirect gets an authorization URL carrying its own ``state``, and the
redirect's ``iss`` is held to the discovered issuer (RFC 9207).

The MCP connection is a stand-in for ``_probe_single_server`` that plays the SDK's part through the
fork's real redirect handler and callback waiter, with the SDK's own ``state`` and ``iss`` checks."""

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

_utils = pytest.importorskip("mcp.client.auth.utils", reason="mcp 2.x SDK not installed")
if not hasattr(_utils, "validate_authorization_response_iss"):
    pytest.skip("mcp 2.x SDK not installed", allow_module_level=True)

from mcp.shared.auth import OAuthMetadata, OAuthToken  # noqa: E402

from tools import mcp_oauth  # noqa: E402

ISSUER = "https://auth.example.com"
REDIRECT = "https://control.example/oauth/callback"
STATE = "caller-state-0123456789abcdef"
SDK_STATE = "sdk-state-fedcba9876543210"
SERVER = {"url": "https://mcp.example.com/mcp"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("IOLLO_VAULT_KEY", raising=False)
    from tools.mcp_oauth_manager import reset_manager_for_tests
    reset_manager_for_tests()

    def probe(name, cfg, connect_timeout=None, **_):
        assert cfg["auth"] == "oauth" and cfg["oauth"]["redirect_uri"] == REDIRECT
        asyncio.run(mcp_oauth._make_redirect_handler(0)(
            f"{ISSUER}/authorize?client_id=c1&redirect_uri={REDIRECT}&state={SDK_STATE}"))
        result = asyncio.run(mcp_oauth._make_callback_waiter(0)())
        assert result.state == SDK_STATE
        _utils.validate_authorization_response_iss(result.iss, OAuthMetadata(
            issuer=ISSUER, authorization_endpoint=f"{ISSUER}/authorize", token_endpoint=f"{ISSUER}/token"))
        asyncio.run(mcp_oauth.HermesTokenStorage(name).set_tokens(
            OAuthToken(access_token=f"access-for-{result.code}", token_type="Bearer")))
        return []

    monkeypatch.setattr("hermes_cli.mcp_config._probe_single_server", probe)
    return tmp_path


def _stored_access(name="notes"):
    tokens = asyncio.run(mcp_oauth.HermesTokenStorage(name).get_tokens())
    return tokens.access_token if tokens else None


def test_start_hands_out_caller_state_and_finish_stores_tokens(home):
    url = mcp_oauth.start_mcp_authorization("notes", REDIRECT, STATE, config=SERVER)
    query = parse_qs(urlparse(url).query)
    assert query["state"] == [STATE] and query["client_id"] == ["c1"]

    with pytest.raises(mcp_oauth.MCPAuthorizationError) as wrong:
        mcp_oauth.finish_mcp_authorization("notes", "code-1", "not-the-state-at-all", iss=ISSUER)
    assert wrong.value.reason == "state_mismatch"

    mcp_oauth.finish_mcp_authorization("notes", "code-1", STATE, iss=ISSUER)
    assert _stored_access() == "access-for-code-1"


def test_mismatched_iss_is_refused_and_previous_tokens_survive(home):
    asyncio.run(mcp_oauth.HermesTokenStorage("notes").set_tokens(
        OAuthToken(access_token="old-access", token_type="Bearer")))
    mcp_oauth.start_mcp_authorization("notes", REDIRECT, STATE, config=SERVER)

    with pytest.raises(mcp_oauth.MCPAuthorizationError) as refused:
        mcp_oauth.finish_mcp_authorization("notes", "code-1", STATE, iss="https://evil.example")
    assert refused.value.reason == "failed"
    assert _stored_access() == "old-access"
    with pytest.raises(mcp_oauth.MCPAuthorizationError) as gone:
        mcp_oauth.finish_mcp_authorization("notes", "code-1", STATE, iss=ISSUER)
    assert gone.value.reason == "no_flow"


def test_http_redirect_uri_only_for_loopback(home):
    with pytest.raises(ValueError):
        mcp_oauth.start_mcp_authorization("notes", "http://control.example/oauth/callback", STATE, config=SERVER)
    for loopback in ("http://127.0.0.1:8765/cb", "http://localhost:8765/cb", "http://[::1]:8765/cb"):
        assert mcp_oauth._is_loopback_host(urlparse(loopback).hostname)


def test_a_newer_start_waits_for_the_older_attempt_to_roll_back(home, monkeypatch):
    """The older attempt's rollback (restore of its snapshot) must be over before the newer one
    touches the server's OAuth state, and the attempt leaves the registry when its worker ends."""
    import time

    events, starts = [], iter(range(1, 10))

    def probe(name, cfg, connect_timeout=None, **_):
        n = next(starts)
        events.append(f"start{n}")
        try:
            asyncio.run(mcp_oauth._make_redirect_handler(0)(
                f"{ISSUER}/authorize?client_id=c1&redirect_uri={REDIRECT}&state={SDK_STATE}{n}"))
            result = asyncio.run(mcp_oauth._make_callback_waiter(0)())
            asyncio.run(mcp_oauth.HermesTokenStorage(name).set_tokens(
                OAuthToken(access_token=f"access-for-{result.code}", token_type="Bearer")))
            return []
        finally:
            time.sleep(0.3)  # a slow wind-down: the older worker is still running after the cancel
            events.append(f"end{n}")

    monkeypatch.setattr("hermes_cli.mcp_config._probe_single_server", probe)
    mcp_oauth.start_mcp_authorization("notes", REDIRECT, STATE, config=SERVER)
    mcp_oauth.start_mcp_authorization("notes", REDIRECT, STATE[::-1], config=SERVER)
    assert events[:3] == ["start1", "end1", "start2"]

    mcp_oauth.finish_mcp_authorization("notes", "code-2", STATE[::-1], iss=ISSUER)
    assert _stored_access() == "access-for-code-2"
    deadline = time.monotonic() + 5
    while mcp_oauth._pending_authorizations and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not mcp_oauth._pending_authorizations


def test_a_start_is_busy_while_the_older_attempt_will_not_end(home, monkeypatch):
    import threading

    release = threading.Event()

    def probe(name, cfg, connect_timeout=None, **_):
        asyncio.run(mcp_oauth._make_redirect_handler(0)(
            f"{ISSUER}/authorize?client_id=c1&redirect_uri={REDIRECT}&state={SDK_STATE}"))
        release.wait(10)  # ignores the cancel
        raise RuntimeError("gave up")

    monkeypatch.setattr("hermes_cli.mcp_config._probe_single_server", probe)
    monkeypatch.setattr(mcp_oauth, "_REPLACE_WAIT_SECONDS", 0.2)
    mcp_oauth.start_mcp_authorization("notes", REDIRECT, STATE, config=SERVER)
    try:
        with pytest.raises(mcp_oauth.MCPAuthorizationError) as busy:
            mcp_oauth.start_mcp_authorization("notes", REDIRECT, STATE[::-1], config=SERVER)
        assert busy.value.reason == "busy"
        with pytest.raises(mcp_oauth.MCPAuthorizationError) as cancelled:
            mcp_oauth.finish_mcp_authorization("notes", "code-1", STATE, iss=ISSUER)
        assert cancelled.value.reason == "no_flow"
    finally:
        release.set()
