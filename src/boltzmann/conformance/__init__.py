"""The conformance suite, importable by third-party implementations.

Two halves, with deliberately opposite requirements. The **golden vectors** are plain JSON
and need nothing at all: an implementation in another language reads them without running
any of this code. The **behavioural suites** are ``pytest`` classes, so they need pytest.

The two are therefore loaded separately. Importing ``golden`` must not drag in a test
framework, or the cross-language half of the kit would be unreachable to exactly the
callers it exists for -- and a plain ``pip install pyboltzmann`` is what those callers have.
"""

from __future__ import annotations

from typing import Any

from boltzmann.conformance import golden

# Every name below is resolved by ``__getattr__``, never imported here. Importing them at module level
# would pull in ``suite``, which imports pytest -- and pytest is an optional extra, so the cross-language
# half of this kit would stop being importable on a plain install. That is what the tests in
# ``test_golden_vectors.py`` pin.
_PYTEST_BACKED = frozenset(
    {
        "BlockStoreConformance",
        "BrainReaderConformance",
        "CompositionConformance",
        "IdentityConformance",
        "MerkleConformance",
        "ReconciliationConformance",
        "sample_blocks",
        "sample_canonical",
        "sample_semantic",
        "sample_semantic_v2",
    }
)


def __getattr__(name: str) -> Any:
    if name not in _PYTEST_BACKED:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        from boltzmann.conformance import suite
    except ModuleNotFoundError as exc:
        if exc.name != "pytest":
            raise
        raise ImportError(
            f"{name} is a pytest class, so inheriting it needs pytest: install "
            "pyboltzmann[conformance]. The golden vectors are unaffected -- "
            "boltzmann.conformance.golden needs nothing and is always available."
        ) from exc
    return getattr(suite, name)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "BlockStoreConformance",
    "BrainReaderConformance",
    "CompositionConformance",
    "IdentityConformance",
    "MerkleConformance",
    "ReconciliationConformance",
    "golden",
    "sample_blocks",
    "sample_canonical",
    "sample_semantic",
    "sample_semantic_v2",
]
