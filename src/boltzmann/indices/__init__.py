"""The index interface. Engines are the implementation's choice, so none ships here."""

from boltzmann.indices.base import AbstractIndex, ContentReader, Index, IndexKind, TravellingIndex

__all__ = [
    "AbstractIndex",
    "ContentReader",
    "Index",
    "IndexKind",
    "TravellingIndex",
]
