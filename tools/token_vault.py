"""Encryption at rest for service tokens: MCP OAuth state (``mcp-tokens/``) and the sealed providers
of ``auth.json`` (Spotify).

Active only when the vault key is set (a managed box): ``IOLLO_VAULT_KEY``, or ``INSTINCT_VAULT_KEY``
under its older name. Each sealed file has its own Fernet key, HKDF-SHA256 of that value with info
``iollo service tokens v1`` + NUL + the file's name relative to the Hermes home (``auth.json.enc``,
``mcp-tokens/<server>.json.enc``), so a sealed file copied over another one does not open. Files
sealed before that binding (one key for every file) still open once and are re-sealed on the spot.
A sealed file is the plaintext file's name plus ``.enc``; an existing plaintext file is migrated into
it on first read and removed. Without the key every caller keeps its plaintext files as before, but
a caller that finds a sealed file with no key refuses (``SealedFileError``) rather than fall back to
plaintext next to it.

Threat model: this protects tokens in volume snapshots, backups and anything that sees the disk
without the running process (a detached volume, a copied image). It does not protect them from code
running as the same user while the agent runs: the key is in the process environment. That is why
the key names are stripped from every child process env (``tools.environments.local_env_policy``),
so shells and tools the agent spawns never inherit them.

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
VAULT_KEY_ENVS = (VAULT_KEY_ENV, "INSTINCT_VAULT_KEY")  # first set one wins
SEALED_SUFFIX = ".enc"
_HKDF_INFO = b"iollo service tokens v1"


def derive_token_key(vault_key: str | bytes, name: str | None = None) -> bytes:
    """The Fernet key for the sealed file *name* (relative to the Hermes home):
    HKDF-SHA256(vault key, info ``iollo service tokens v1`` NUL *name*). ``name=None`` is the
    unbound key files were sealed with before the binding; it only ever opens old files."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw = vault_key.encode() if isinstance(vault_key, str) else vault_key
    info = _HKDF_INFO if name is None else _HKDF_INFO + b"\0" + name.encode("utf-8")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(raw)
    return base64.urlsafe_b64encode(derived)


@functools.lru_cache(maxsize=256)
def _fernet_for(vault_key: str, name: str | None):
    from cryptography.fernet import Fernet

    return Fernet(derive_token_key(vault_key, name))


def _vault_key() -> str | None:
    for env in VAULT_KEY_ENVS:
        if value := os.environ.get(env):
            return value
    return None


def vault_fernet(name: str | None = None):
    """The Fernet for sealed file *name* when a vault key is set, else None (read on every call)."""
    vault_key = _vault_key()
    return _fernet_for(vault_key, name) if vault_key else None


def vault_active() -> bool:
    return _vault_key() is not None


def sealed_path(path: Path) -> Path:
    """``<name>`` -> ``<name>.enc``."""
    return path.with_name(path.name + SEALED_SUFFIX)


def is_sealed(path: Path) -> bool:
    return path.name.endswith(SEALED_SUFFIX)


def plaintext_path(path: Path) -> Path:
    """``<name>.enc`` -> ``<name>``."""
    return path.with_name(path.name[: -len(SEALED_SUFFIX)]) if is_sealed(path) else path


class SealedFileError(ValueError):
    """A sealed file exists but cannot be opened (no key, wrong key, truncated, not ours)."""


def require_vault_for(path: Path) -> None:
    """Raise ``SealedFileError`` when *path* has a sealed copy (``<path>.enc``) and no vault key is
    set: a caller must then neither read the stale plaintext nor write plaintext next to the sealed
    file (the next start with the key would find two diverging copies)."""
    if not vault_active() and not is_sealed(path) and sealed_path(path).exists():
        raise SealedFileError(f"{sealed_path(path).name} is sealed and no vault key "
                              f"({' or '.join(VAULT_KEY_ENVS)}) is set")


def read_sealed_json(path: Path, *, name: str | None = None) -> Any:
    """Decrypt and parse *path*; None when absent. *name* is the file's name relative to the Hermes
    home (default: its base name) and must match the one it was written with. Raises
    ``SealedFileError`` when no vault key is set or the key does not open the file, so a caller
    never mistakes a sealed store for an empty one. A file sealed before per-file keys is re-sealed
    under its own key the first time it is read."""
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        return None
    name = name or path.name
    fernet = vault_fernet(name)
    if fernet is None:
        raise SealedFileError(f"{path.name} is sealed and no vault key ({' or '.join(VAULT_KEY_ENVS)}) is set")
    from cryptography.fernet import InvalidToken

    try:
        return _parse(fernet.decrypt(blob))
    except InvalidToken:
        pass
    except ValueError as exc:
        raise SealedFileError(f"{path.name} could not be opened with the vault key") from exc
    try:
        data = _parse(vault_fernet(None).decrypt(blob))
    except (InvalidToken, ValueError) as exc:
        raise SealedFileError(f"{path.name} could not be opened with the vault key") from exc
    write_sealed_json(path, data, name=name)
    logger.info("token vault: re-sealed %s under its own key", path.name)
    return data


def _parse(payload: bytes) -> Any:
    return json.loads(payload.decode("utf-8"))


def write_sealed_json(path: Path, data: Any, *, name: str | None = None) -> None:
    """Encrypt *data* as JSON into *path* atomically, 0600 from creation, under the key for *name*
    (the file's name relative to the Hermes home; default: its base name)."""
    from utils import atomic_write_bytes

    fernet = vault_fernet(name or path.name)
    if fernet is None:
        raise SealedFileError(f"cannot seal {path.name}: no vault key ({' or '.join(VAULT_KEY_ENVS)}) is set")
    payload = json.dumps(data, default=str, ensure_ascii=True).encode("utf-8")
    atomic_write_bytes(path, fernet.encrypt(payload), mode=0o600)


def migrate_plaintext(sealed: Path, *, name: str | None = None) -> None:
    """Move the plaintext sibling of *sealed* into it and remove the plaintext file.

    With no sealed file yet, the plaintext is sealed. With both present, the newer one wins: a
    plaintext copy older than the sealed file is a stale leftover and is removed; one that is newer
    (written while the key was missing) is read and sealed over the old sealed file, never dropped
    unread. Unparseable plaintext is left in place (the caller's normal corrupt-file handling reports
    it). No-op when the vault is off or there is no plaintext."""
    if not vault_active():
        return
    plain = plaintext_path(sealed)
    if plain == sealed or not plain.exists():
        return
    try:
        stale = sealed.exists() and plain.stat().st_mtime_ns <= sealed.stat().st_mtime_ns
    except FileNotFoundError:
        stale = False
    if not stale:
        try:
            data = json.loads(plain.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            logger.warning("token vault: could not migrate %s (%s); leaving it in place", plain.name,
                           type(exc).__name__)
            return
        write_sealed_json(sealed, data, name=name)
        logger.info("token vault: sealed %s into %s", plain.name, sealed.name)
    plain.unlink(missing_ok=True)
