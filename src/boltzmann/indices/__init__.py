"""The index interface. Engines are the implementation's choice, so none ships here."""

from boltzmann.indices.base import AbstractIndex, Index, IndexKind

__all__ = [
    "AbstractIndex",
    "Index",
    "IndexKind",
]
