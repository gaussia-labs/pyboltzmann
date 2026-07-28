"""Packing a module into an OCI layer, and unpacking one back.

**One blob per module is a necessary condition, not an optimization.** Selection is only efficient if
each module is a separate OCI blob: if everything is mixed into a single file, the whole file must be
downloaded (paper Section 7.2). So a layer holds exactly one module, and pulling it gives a consumer
that module and nothing else.

**What a layer must contain.** The composition document, so the version can be reopened, plus every
blob the composition needs to be self-sufficient: each block's envelope, and -- for the canonical
module -- the observed bytes each canonical block describes, together with any normalized view. A
canonical layer without the originals would arrive as a set of claims about evidence the consumer
cannot read.

**Why the packing is deterministic.** A layer is content-addressed, so two clients packing the same
module must produce the same digest, or push deduplication silently stops working and every push
re-uploads everything. ``tarfile`` and ``gzip`` both embed timestamps and ownership by default, so
every one of those fields is pinned here and entries are written in sorted order.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from typing import TYPE_CHECKING

from boltzmann.blocks.canonical import CanonicalBlock
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.exceptions import DistributionError
from boltzmann.identity.digest import BlockId, Digest, OciDigest
from boltzmann.module.composition import Composition

if TYPE_CHECKING:
    from boltzmann.module.module import Module
    from boltzmann.store.base import BlockStore

COMPOSITION_ENTRY = "composition.json"
"""Name of the composition document inside a layer."""

BLOB_PREFIX = "blobs/"
"""Directory inside a layer holding content-addressed blobs, named by hex digest."""

GZIP_LEVEL = 9
"""Compression level. Fixed, because it is part of what makes the layer digest reproducible."""


def required_blobs(module: Module) -> list[Digest]:
    """
    Every blob a layer must carry for this module to be self-sufficient.

    Args:
        module (Module): The module version to pack.

    Returns:
        list[Digest]: The block envelopes, plus the observed bytes and normalized views that canonical
        blocks describe. Sorted, so the answer does not depend on iteration order.
    """
    needed: list[Digest] = list(module.composition.block_ids)

    if module.memory_type is MemoryType.CANONICAL:
        for block_id in module.composition.block_ids:
            if not module.store.is_resolvable(block_id):
                continue
            block = module.get(block_id)
            if not isinstance(block, CanonicalBlock):
                continue
            needed.append(block.blob)
            if block.normalized_view is not None:
                needed.append(block.normalized_view.blob)

    unique = {digest.hex: digest for digest in needed}
    return [unique[key] for key in sorted(unique)]


def pack_module(module: Module) -> bytes:
    """
    Pack one module into a layer blob.

    Args:
        module (Module): The module version to pack.

    Returns:
        bytes: The gzipped tar, byte-identical for any client packing the same composition.

    Raises:
        DistributionError: If a block the composition names cannot be read, because a layer that
            shipped an unresolvable block would name knowledge the consumer could never verify.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        _add(archive, COMPOSITION_ENTRY, module.composition.document())
        for digest in required_blobs(module):
            try:
                payload = module.store.get_bytes(digest)
            except Exception as error:
                raise DistributionError(
                    f"cannot pack the {module.memory_type.value} module: {digest.short} is named by the "
                    f"composition but cannot be read ({error})"
                ) from error
            _add(archive, f"{BLOB_PREFIX}{digest.hex}", payload)

    compressed = io.BytesIO()
    # mtime=0 so the gzip header carries no timestamp.
    with gzip.GzipFile(fileobj=compressed, mode="wb", compresslevel=GZIP_LEVEL, mtime=0) as stream:
        stream.write(raw.getvalue())
    return compressed.getvalue()


def _add(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    """Append one entry with every variable field pinned."""
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.type = tarfile.REGTYPE
    archive.addfile(info, io.BytesIO(payload))


def unpack_layer(data: bytes, store: BlockStore) -> Composition:
    """
    Unpack a layer into a store and recover the composition it carries.

    Every blob is written through the store's content-addressed put, so a layer whose bytes were
    tampered with cannot land under the digest it claims: the digest is recomputed from the bytes.
    The recovered composition is then checked against the blobs actually present.

    Args:
        data (bytes): The layer blob.
        store (BlockStore): Where to write the unpacked blobs.

    Returns:
        Composition: The composition the layer carries.

    Raises:
        DistributionError: If the layer is malformed, lacks its composition document, or names a block
            it did not carry.
    """
    entries: dict[str, bytes] = {}
    try:
        with (
            gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream,
            tarfile.open(fileobj=io.BytesIO(stream.read()), mode="r") as archive,
        ):
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is not None:
                    entries[member.name] = extracted.read()
    except (OSError, EOFError, tarfile.TarError) as error:
        # EOFError is what a truncated gzip stream raises, and it is not an OSError.
        raise DistributionError(f"layer is not a readable gzipped tar: {error}") from error

    document = entries.pop(COMPOSITION_ENTRY, None)
    if document is None:
        raise DistributionError(f"layer carries no {COMPOSITION_ENTRY}, so its version cannot be reopened")

    # The composition document is itself content-addressed, and the snapshot's ModuleRef names it by
    # digest. Storing it is what makes the pulled version reopenable rather than merely unpacked.
    store.put_bytes(document)

    for name, payload in entries.items():
        if not name.startswith(BLOB_PREFIX):
            raise DistributionError(f"layer carries an unexpected entry {name!r}")
        stored = store.put_bytes(payload)
        if stored.hex != name.removeprefix(BLOB_PREFIX):
            raise DistributionError(
                f"layer entry {name!r} holds bytes that hash to {stored.short}: the layer is corrupt"
            )

    composition = Composition.from_document(document)
    missing = [block_id.short for block_id in composition.block_ids if not store.has(block_id)]
    if missing:
        raise DistributionError(f"layer's composition names blocks the layer did not carry: {', '.join(missing)}")
    return composition


def layer_digest(data: bytes) -> OciDigest:
    """
    Content address of a packed layer.

    Args:
        data (bytes): The layer blob.

    Returns:
        OciDigest: Its digest, which is what a manifest descriptor names.
    """
    return OciDigest.of(data)


def blob_ids_in(composition: Composition) -> list[BlockId]:
    """
    The block identities a composition commits to.

    Args:
        composition (Composition): The composition.

    Returns:
        list[BlockId]: Its blocks, in canonical leaf order.
    """
    return composition.block_ids
