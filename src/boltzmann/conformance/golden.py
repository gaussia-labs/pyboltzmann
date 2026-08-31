"""Golden vectors: the protocol's cross-language test fixtures.

The corpus is **not owned here**. It is published as spec-level data at
``gaussia-labs/boltzmann-conformance`` and vendored into this package at the version
:data:`CORPUS_VERSION` names, so a plain ``pip install`` still carries it and a reader in another
language still needs no Python at all. The authority is the corpus; this SDK is one of its
consumers, and ``tests/test_golden_vectors.py`` is where it demonstrates that it agrees.

That split is the point. While these files were maintained here, their location, naming and shape
were governed by this package's layout, and "conforming" quietly degraded into "matches pyboltzmann,
bugs included". A vector this SDK cannot reproduce is now a disagreement to resolve rather than a
file to edit.

Once published a vector never changes: a changed vector means either a bug, or a new serialization
identifier. Cases are added, and adding them bumps the corpus version.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

CORPUS_VERSION = "1.1"
"""Which published corpus this package carries.

``<protocol>.<revision>``, matching ``CORPUS_VERSION`` in the corpus repository. A CI job compares
the two, so a vendored copy that drifts from what was published fails loudly rather than quietly
becoming this SDK's private opinion again.
"""

CORPUS_REPOSITORY = "https://github.com/gaussia-labs/boltzmann-conformance"
"""Where the corpus is published, and where a disagreement with it gets resolved."""

VECTOR_FILES = (
    "block_ids.json",
    "actor_ids.json",
    "schema_selection.json",
    "merkle_roots.json",
    "inclusion_proofs.json",
    "serialization.json",
    "sshsig.json",
    "signatures.json",
    "reconciliation.json",
)
"""The published vector files, in the order the paper lists the categories."""

REGISTRY_FILE = "schemas.json"
"""The schema registry companion: which block schemas are registered, and under which versions."""


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


def registry() -> Any:
    """
    Load the schema registry companion.

    A schema is registered exactly when this document carries it, and *oldest* in the
    oldest-that-fits rule means by the ``schema_version`` recorded here (paper Section 6.6). A
    per-deployment registry would make identity comparable only within a deployment, since the
    version sits inside ``block_id``.

    Returns:
        Any: The parsed companion document.
    """
    source = resources.files("boltzmann.conformance.registry") / REGISTRY_FILE
    return json.loads(source.read_text(encoding="utf-8"))


def load_all() -> dict[str, Any]:
    """
    Load every vector file.

    Returns:
        dict[str, Any]: Vectors keyed by file name.
    """
    return {name: load(name) for name in VECTOR_FILES}
