"""Sentence embeddings for the iollo_notes index: the pinned all-MiniLM-L6-v2 int8 ONNX model.

The model and its tokenizer ship inside the release (box image and Mac runtime tarball) at
``plugins/memory/iollo_notes/model/``, fetched at build time by
``scripts/iollo/fetch-embedding-model.py`` and checked against ``model.json``. Nothing is ever
downloaded at runtime: a missing file, a hash mismatch or a missing extra means no embedder, and
search runs on FTS alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from pathlib import Path
from typing import List, Optional, Protocol, Sequence

logger = logging.getLogger(__name__)

MODEL_SPEC_PATH = Path(__file__).with_name("model.json")
DEFAULT_MODEL_DIR = Path(__file__).with_name("model")
MODEL_DIR_ENV = "IOLLO_EMBED_MODEL_DIR"


class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Unit-length vectors, one per text."""


def load_spec() -> dict:
    return json.loads(MODEL_SPEC_PATH.read_text(encoding="utf-8"))


def model_dir() -> Path:
    override = os.environ.get(MODEL_DIR_ENV, "").strip()
    return Path(override) if override else DEFAULT_MODEL_DIR


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_model_dir(directory: Path, spec: Optional[dict] = None) -> List[str]:
    """Problems with the model files in ``directory`` against the pinned spec (empty = good)."""
    spec = spec or load_spec()
    problems = []
    for name, meta in spec["files"].items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: missing")
        elif path.stat().st_size != meta["size"]:
            problems.append(f"{name}: size mismatch")
        elif sha256_file(path) != meta["sha256"]:
            problems.append(f"{name}: sha256 mismatch")
    return problems


def normalize(vec: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [float(x) / norm for x in vec]


class OnnxEmbedder:
    """Mean-pooled, L2-normalised MiniLM embeddings via onnxruntime + tokenizers."""

    def __init__(self, directory: Path, spec: dict):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        self.dim = int(spec["dim"])
        self.max_tokens = int(spec["max_tokens"])
        self._tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=self.max_tokens)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # a box is small; the Mac runs this beside the UI
        options.log_severity_level = 3
        self._session = ort.InferenceSession(str(directory / "model.onnx"), sess_options=options,
                                             providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._lock = threading.Lock()

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        np = self._np
        encodings = self._tokenizer.encode_batch([t or " " for t in texts])
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.zeros_like(ids)
        with self._lock:
            hidden = self._session.run(None, {k: v for k, v in feeds.items() if k in self._inputs})[0]
        weights = mask[:, :, None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
        pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
        return pooled.astype(np.float32).tolist()


_cache_lock = threading.Lock()
_cache: dict = {}
_warned: set = set()


def _warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def load_embedder() -> Optional[Embedder]:
    """The shipped model for this process (cached per directory), or None when it cannot run."""
    directory = model_dir()
    key = str(directory.resolve()) if directory.exists() else str(directory)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
        embedder = None
        try:
            spec = load_spec()
            problems = verify_model_dir(directory, spec)
            if problems:
                _warn_once("embed_unavailable:" + key, "iollo_notes embed_unavailable: %s; search uses FTS only",
                           "; ".join(problems))
            else:
                embedder = OnnxEmbedder(directory, spec)
        except ImportError as exc:
            _warn_once("embed_unavailable:deps", "iollo_notes embed_unavailable: %s (install the iollo-memory "
                       "extra); search uses FTS only", exc.name or exc)
        except Exception as exc:  # corrupt model, runtime failure: degrade, never break the agent
            _warn_once("embed_unavailable:" + key, "iollo_notes embed_unavailable: %s; search uses FTS only",
                       type(exc).__name__)
        _cache[key] = embedder
        return embedder
