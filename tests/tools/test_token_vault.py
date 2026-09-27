"""Sealed MCP OAuth state (``tools.token_vault``): with ``IOLLO_VAULT_KEY`` set the token files are
Fernet-encrypted ``.enc`` files and plaintext left from before is migrated; without it nothing
changes."""

import asyncio
import json

import pytest

pytest.importorskip("mcp.shared.auth", reason="MCP SDK required")

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken  # noqa: E402

from tools.mcp_oauth import HermesTokenStorage  # noqa: E402

VAULT_KEY = "test-vault-key-not-a-secret"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("IOLLO_VAULT_KEY", raising=False)
    return tmp_path


def _tokens(access="access-abc"):
    return OAuthToken(access_token=access, token_type="Bearer", refresh_token="refresh-xyz", expires_in=3600)


def test_sealed_tokens_never_touch_disk_in_plaintext(home, monkeypatch):
    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    storage = HermesTokenStorage("srv")
    asyncio.run(storage.set_tokens(_tokens()))

    token_dir = home / "mcp-tokens"
    assert sorted(p.name for p in token_dir.iterdir() if not p.name.endswith(".lock")) == ["srv.json.enc"]
    blob = (token_dir / "srv.json.enc").read_bytes()
    assert b"access-abc" not in blob and b"refresh-xyz" not in blob
    assert (token_dir / "srv.json.enc").stat().st_mode & 0o777 == 0o600
    loaded = asyncio.run(HermesTokenStorage("srv").get_tokens())
    assert loaded.access_token == "access-abc" and loaded.refresh_token == "refresh-xyz"
    assert HermesTokenStorage("srv").has_cached_tokens()

    monkeypatch.setenv("IOLLO_VAULT_KEY", "another-key")
    assert asyncio.run(HermesTokenStorage("srv").get_tokens()) is None


def test_plaintext_state_is_migrated_on_first_read_and_removed(home, monkeypatch):
    asyncio.run(HermesTokenStorage("srv").set_tokens(_tokens()))
    asyncio.run(HermesTokenStorage("srv").set_client_info(OAuthClientInformationFull(
        client_id="client-1", redirect_uris=["https://box.example/oauth/callback"])))
    token_dir = home / "mcp-tokens"
    assert (token_dir / "srv.json").exists() and (token_dir / "srv.client.json").exists()

    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    storage = HermesTokenStorage("srv")
    assert asyncio.run(storage.get_tokens()).access_token == "access-abc"
    assert asyncio.run(storage.get_client_info()).client_id == "client-1"
    assert not (token_dir / "srv.json").exists() and not (token_dir / "srv.client.json").exists()
    assert (token_dir / "srv.json.enc").exists() and (token_dir / "srv.client.json.enc").exists()

    storage.remove()
    assert not [p for p in token_dir.iterdir() if not p.name.endswith(".lock")]


def test_without_vault_key_token_files_stay_plaintext(home):
    asyncio.run(HermesTokenStorage("srv").set_tokens(_tokens()))
    token_dir = home / "mcp-tokens"
    assert not list(token_dir.glob("*.enc"))
    assert json.loads((token_dir / "srv.json").read_text())["access_token"] == "access-abc"
