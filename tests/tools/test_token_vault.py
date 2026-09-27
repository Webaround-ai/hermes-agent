"""Sealed MCP OAuth state (``tools.token_vault``): with ``IOLLO_VAULT_KEY`` set the token files are
Fernet-encrypted ``.enc`` files and plaintext left from before is migrated; without it nothing
changes."""

import asyncio
import json

import pytest

pytest.importorskip("mcp.shared.auth", reason="MCP SDK required")

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken  # noqa: E402

from tools import token_vault  # noqa: E402
from tools.mcp_oauth import HermesTokenStorage  # noqa: E402

VAULT_KEY = "test-vault-key-not-a-secret"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("IOLLO_VAULT_KEY", raising=False)
    monkeypatch.delenv("INSTINCT_VAULT_KEY", raising=False)
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


def test_older_key_name_opens_the_same_vault(home, monkeypatch):
    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    asyncio.run(HermesTokenStorage("srv").set_tokens(_tokens()))

    monkeypatch.delenv("IOLLO_VAULT_KEY")
    monkeypatch.setenv("INSTINCT_VAULT_KEY", VAULT_KEY)
    assert token_vault.vault_active()
    assert asyncio.run(HermesTokenStorage("srv").get_tokens()).access_token == "access-abc"


def test_sealed_state_without_the_key_is_refused_not_replaced_by_plaintext(home, monkeypatch):
    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    asyncio.run(HermesTokenStorage("srv").set_tokens(_tokens()))
    token_dir = home / "mcp-tokens"
    sealed_before = (token_dir / "srv.json.enc").read_bytes()

    monkeypatch.delenv("IOLLO_VAULT_KEY")
    storage = HermesTokenStorage("srv")
    with pytest.raises(token_vault.SealedFileError):
        asyncio.run(storage.set_tokens(_tokens("new-access")))
    with pytest.raises(token_vault.SealedFileError):
        asyncio.run(storage.get_tokens())
    with pytest.raises(token_vault.SealedFileError):
        storage.has_cached_tokens()
    assert not (token_dir / "srv.json").exists()
    assert (token_dir / "srv.json.enc").read_bytes() == sealed_before


def _plain_and_sealed(home, monkeypatch, *, plaintext_newer):
    import os

    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    asyncio.run(HermesTokenStorage("srv").set_tokens(_tokens("sealed-access")))
    monkeypatch.delenv("IOLLO_VAULT_KEY")
    token_dir = home / "mcp-tokens"
    plain, sealed = token_dir / "srv.json", token_dir / "srv.json.enc"
    plain.write_text(json.dumps({"access_token": "plain-access", "token_type": "Bearer"}))
    stamp = sealed.stat().st_mtime_ns
    offset = 10**9 if plaintext_newer else -(10**9)
    os.utime(plain, ns=(stamp + offset, stamp + offset))
    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    return plain, sealed


def test_plaintext_newer_than_the_sealed_file_is_migrated_not_dropped(home, monkeypatch):
    plain, sealed = _plain_and_sealed(home, monkeypatch, plaintext_newer=True)
    assert asyncio.run(HermesTokenStorage("srv").get_tokens()).access_token == "plain-access"
    assert not plain.exists() and sealed.exists()


def test_plaintext_older_than_the_sealed_file_is_a_stale_leftover(home, monkeypatch):
    plain, sealed = _plain_and_sealed(home, monkeypatch, plaintext_newer=False)
    assert asyncio.run(HermesTokenStorage("srv").get_tokens()).access_token == "sealed-access"
    assert not plain.exists()


def test_a_sealed_file_only_opens_under_its_own_name(home, monkeypatch):
    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    asyncio.run(HermesTokenStorage("victim").set_tokens(_tokens("victim-access")))
    asyncio.run(HermesTokenStorage("attacker").set_tokens(_tokens("attacker-access")))
    token_dir = home / "mcp-tokens"
    (token_dir / "victim.json.enc").write_bytes((token_dir / "attacker.json.enc").read_bytes())
    assert asyncio.run(HermesTokenStorage("victim").get_tokens()) is None


def test_file_sealed_before_per_file_keys_is_read_once_and_resealed(home, monkeypatch):
    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    token_dir = home / "mcp-tokens"
    token_dir.mkdir()
    legacy = token_vault.vault_fernet(None).encrypt(
        json.dumps({"access_token": "legacy-access", "token_type": "Bearer"}).encode())
    (token_dir / "srv.json.enc").write_bytes(legacy)

    assert asyncio.run(HermesTokenStorage("srv").get_tokens()).access_token == "legacy-access"
    resealed = (token_dir / "srv.json.enc").read_bytes()
    assert resealed != legacy
    assert token_vault.vault_fernet("mcp-tokens/srv.json.enc").decrypt(resealed)
