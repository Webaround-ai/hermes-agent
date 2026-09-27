"""Encryption at rest for service tokens: MCP OAuth state (``mcp-tokens/``) and the sealed providers
of ``auth.json`` (Spotify).

Active only when ``IOLLO_VAULT_KEY`` is set (a managed box). The Fernet key is HKDF-SHA256 of that
value with info ``iollo service tokens v1``, never the value itself. A sealed file is the plaintext
file's name plus ``.enc``; an existing plaintext file is migrated into it on first read and
removed. Without the env var every caller keeps its plaintext files exactly as before.

Nothing here logs file contents, keys or tokens."""

from __future__ import annotations

import base64
import functools
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VAULT_KEY_ENV = "IOLLO_VAULT_KEY"
SEALED_SUFFIX = ".enc"
_HKDF_INFO = b"iollo service tokens v1"


def derive_token_key(vault_key: str | bytes) -> bytes:
    """The Fernet key for service tokens: HKDF-SHA256(vault key, info ``iollo service tokens v1``)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw = vault_key.encode() if isinstance(vault_key, str) else vault_key
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(raw)
    return base64.urlsafe_b64encode(derived)


@functools.lru_cache(maxsize=4)
def _fernet_for(vault_key: str):
    from cryptography.fernet import Fernet

    return Fernet(derive_token_key(vault_key))


def vault_fernet():
    """The token Fernet when ``IOLLO_VAULT_KEY`` is set, else None (read on every call)."""
    vault_key = os.environ.get(VAULT_KEY_ENV)
    return _fernet_for(vault_key) if vault_key else None


def vault_active() -> bool:
    return bool(os.environ.get(VAULT_KEY_ENV))


def sealed_path(path: Path) -> Path:
    """``<name>`` -> ``<name>.enc``."""
    return path.with_name(path.name + SEALED_SUFFIX)


def is_sealed(path: Path) -> bool:
    return path.name.endswith(SEALED_SUFFIX)


def plaintext_path(path: Path) -> Path:
    """``<name>.enc`` -> ``<name>``."""
    return path.with_name(path.name[: -len(SEALED_SUFFIX)]) if is_sealed(path) else path


class SealedFileError(ValueError):
    """A sealed file exists but cannot be opened (wrong key, truncated, not ours)."""


def read_sealed_json(path: Path) -> Any:
    """Decrypt and parse *path*; None when absent. Raises ``SealedFileError`` when the vault key is
    missing or does not open the file, so a caller never mistakes a sealed store for an empty one."""
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        return None
    fernet = vault_fernet()
    if fernet is None:
        raise SealedFileError(f"{path.name} is sealed and {VAULT_KEY_ENV} is not set")
    from cryptography.fernet import InvalidToken

    try:
        return json.loads(fernet.decrypt(blob).decode("utf-8"))
    except (InvalidToken, ValueError) as exc:
        raise SealedFileError(f"{path.name} could not be opened with the vault key") from exc


def write_sealed_json(path: Path, data: Any) -> None:
    """Encrypt *data* as JSON into *path* atomically, 0600 from creation."""
    from utils import atomic_write_bytes

    fernet = vault_fernet()
    if fernet is None:
        raise SealedFileError(f"cannot seal {path.name}: {VAULT_KEY_ENV} is not set")
    payload = json.dumps(data, default=str, ensure_ascii=True).encode("utf-8")
    atomic_write_bytes(path, fernet.encrypt(payload), mode=0o600)


def migrate_plaintext(sealed: Path) -> None:
    """Move the plaintext sibling of *sealed* into it and remove the plaintext file.

    A sealed file that already exists wins: a plaintext copy next to it is a stale leftover and is
    removed without being read. Unparseable plaintext is left in place (the caller's normal
    corrupt-file handling reports it). No-op when the vault is off or there is no plaintext."""
    if not vault_active():
        return
    plain = plaintext_path(sealed)
    if plain == sealed or not plain.exists():
        return
    if not sealed.exists():
        try:
            data = json.loads(plain.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            logger.warning("token vault: could not migrate %s (%s); leaving it in place", plain.name,
                           type(exc).__name__)
            return
        write_sealed_json(sealed, data)
        logger.info("token vault: sealed %s into %s", plain.name, sealed.name)
    plain.unlink(missing_ok=True)
