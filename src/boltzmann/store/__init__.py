"""Content-addressed persistence: blobs on the physical level, blocks on the knowledge level."""

from boltzmann.store.base import AbstractBlockStore, BlockStore
from boltzmann.store.memory import MemoryBlockStore
from boltzmann.store.oci_layout import IMAGE_LAYOUT_VERSION, OciLayoutStore

__all__ = [
    "IMAGE_LAYOUT_VERSION",
    "AbstractBlockStore",
    "BlockStore",
    "MemoryBlockStore",
    "OciLayoutStore",
]
