"""Sealed providers of the auth store (``hermes_cli.auth_sealed``): with ``IOLLO_VAULT_KEY`` set the
Spotify login lives in ``auth.json.enc``, never in ``auth.json``; without it nothing changes."""

import json

import pytest

from hermes_cli import auth as auth_mod

VAULT_KEY = "test-vault-key-not-a-secret"
SPOTIFY = {"client_id": "spotify-client", "access_token": "sp-access", "refresh_token": "sp-refresh"}
OTHER = {"access_token": "other-access"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("IOLLO_VAULT_KEY", raising=False)
    return tmp_path


def _save(**providers):
    with auth_mod._auth_store_lock():
        store = auth_mod._load_auth_store()
        for name, state in providers.items():
            auth_mod._store_provider_state(store, name, state, set_active=False)
        auth_mod._save_auth_store(store)


def test_spotify_is_sealed_and_other_providers_stay_plaintext(home, monkeypatch):
    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    _save(spotify=SPOTIFY, nous=OTHER)

    plain = (home / "auth.json").read_text()
    assert "sp-access" not in plain and "sp-refresh" not in plain
    assert json.loads(plain)["providers"] == {"nous": OTHER}
    assert b"sp-access" not in (home / "auth.json.enc").read_bytes()
    assert auth_mod.get_provider_auth_state("spotify") == SPOTIFY
    assert auth_mod.get_provider_auth_state("nous") == OTHER

    assert auth_mod.clear_provider_auth("spotify")
    assert not (home / "auth.json.enc").exists()
    assert auth_mod.get_provider_auth_state("nous") == OTHER


def test_plaintext_spotify_login_is_migrated_on_first_read(home, monkeypatch):
    _save(spotify=SPOTIFY, nous=OTHER)
    assert "sp-access" in (home / "auth.json").read_text()

    monkeypatch.setenv("IOLLO_VAULT_KEY", VAULT_KEY)
    assert auth_mod.get_provider_auth_state("spotify") == SPOTIFY
    assert "sp-access" not in (home / "auth.json").read_text()
    assert (home / "auth.json.enc").exists()
    assert auth_mod.get_provider_auth_state("nous") == OTHER


def test_without_vault_key_spotify_stays_in_auth_json(home):
    _save(spotify=SPOTIFY)
    assert json.loads((home / "auth.json").read_text())["providers"]["spotify"] == SPOTIFY
    assert not (home / "auth.json.enc").exists()
