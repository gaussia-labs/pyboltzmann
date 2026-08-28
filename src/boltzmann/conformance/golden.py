"""Golden vectors: the protocol's cross-language test fixtures.

The vectors under ``vectors/`` are plain JSON, and they ship inside the wheel. An
implementation in another language reads the same files and must reach the same
``block_id`` and ``MerkleRoot`` values -- which is the only practical way to check that
two clients really agree on identity, rather than merely claiming to.

They are generated from this SDK by :func:`regenerate`, and once published a vector file
must not change: a changed vector means either a bug, or a new serialization identifier.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

VECTOR_FILES = (
    "block_ids.json",
    "merkle_roots.json",
    "inclusion_proofs.json",
    "serialization.json",
    "sshsig.json",
    "signatures.json",
)
"""The published vector files."""


def load(name: str) -> Any:
    """
    Load one vector file.

    Args:
        name (str): File name, such as ``"block_ids.json"``.

    Returns:
        Any: The parsed vectors.
    """
    source = resources.files("boltzmann.conformance.vectors") / name
    return json.loads(source.read_text(encoding="utf-8"))


def load_all() -> dict[str, Any]:
    """
    Load every vector file.

    Returns:
        dict[str, Any]: Vectors keyed by file name.
    """
    return {name: load(name) for name in VECTOR_FILES}
