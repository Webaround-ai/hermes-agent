"""Sealed providers of the auth store: with ``IOLLO_VAULT_KEY`` set, the state of the providers in
``SEALED_AUTH_PROVIDERS`` (the Spotify login) lives in ``auth.json.enc`` (``tools.token_vault``)
instead of ``auth.json``. ``_load_auth_store`` overlays them, ``_save_auth_store`` splits them out,
so every reader and writer of the store keeps working unchanged. Without the env var this module
does nothing and ``auth.json`` is exactly what it was."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict

from tools import token_vault

logger = logging.getLogger(__name__)

SEALED_AUTH_PROVIDERS = frozenset({"spotify"})


def _sealed_providers(auth_file: Path) -> Dict[str, Any]:
    """Providers in ``<auth_file>.enc``; raises ``SealedFileError`` when it cannot be opened."""
    data = token_vault.read_sealed_json(token_vault.sealed_path(auth_file))
    providers = data.get("providers") if isinstance(data, dict) else None
    return dict(providers) if isinstance(providers, dict) else {}


def overlay_sealed_providers(
    auth_file: Path, store: Dict[str, Any], *, lock: Callable, load_plain: Callable, save_plain: Callable,
) -> Dict[str, Any]:
    """Merge the sealed providers into *store* (sealed state wins). A sealed provider still in the
    plaintext file is migrated first, under the store lock, and removed from ``auth.json``."""
    if not token_vault.vault_active():
        return store
    providers = store.get("providers")
    if not isinstance(providers, dict):
        return store
    try:
        if SEALED_AUTH_PROVIDERS & providers.keys():
            with lock(target_path=auth_file):
                plain = load_plain(auth_file)
                plain_providers = plain.get("providers") if isinstance(plain.get("providers"), dict) else {}
                leftover = {k: plain_providers.pop(k) for k in SEALED_AUTH_PROVIDERS & plain_providers.keys()}
                if leftover:
                    sealed = {**leftover, **_sealed_providers(auth_file)}
                    write_sealed_providers(auth_file, sealed)
                    save_plain(auth_file, plain)
                    logger.info("auth: sealed %s into %s", ", ".join(sorted(leftover)),
                                token_vault.sealed_path(auth_file).name)
            for key in SEALED_AUTH_PROVIDERS:
                providers.pop(key, None)
        providers.update(_sealed_providers(auth_file))
    except token_vault.SealedFileError as exc:
        logger.warning("auth: %s", exc)
    return store


def write_sealed_providers(auth_file: Path, sealed: Dict[str, Any]) -> None:
    from hermes_cli.auth import AUTH_STORE_VERSION

    token_vault.write_sealed_json(token_vault.sealed_path(auth_file),
                                  {"version": AUTH_STORE_VERSION, "providers": sealed})


def split_sealed_providers(auth_file: Path, store: Dict[str, Any]) -> Dict[str, Any]:
    """Write *store*'s sealed providers to ``<auth_file>.enc`` and return the copy of *store* that
    goes to ``auth.json`` (without them). *store* itself is not changed. A store with no sealed
    provider removes the sealed file (a logout), unless that file cannot be opened: then it is left
    alone rather than destroyed by a process that never saw its contents."""
    providers = store.get("providers")
    if not token_vault.vault_active() or not isinstance(providers, dict):
        return store
    sealed = {k: v for k, v in providers.items() if k in SEALED_AUTH_PROVIDERS}
    if sealed:
        write_sealed_providers(auth_file, sealed)
    else:
        sealed_file = token_vault.sealed_path(auth_file)
        try:
            token_vault.read_sealed_json(sealed_file)
        except token_vault.SealedFileError as exc:
            logger.warning("auth: leaving %s in place: %s", sealed_file.name, exc)
        else:
            sealed_file.unlink(missing_ok=True)
    return {**store, "providers": {k: v for k, v in providers.items() if k not in SEALED_AUTH_PROVIDERS}}
