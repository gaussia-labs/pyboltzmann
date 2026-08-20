"""Packing a module into an OCI layer, and unpacking one back.

**One blob per module is a necessary condition, not an optimization.** Selection is only efficient if
each module is a separate OCI blob: if everything is mixed into a single file, the whole file must be
downloaded (paper Section 7.2). So a layer holds exactly one module, and pulling it gives a consumer
that module and nothing else.

**What a layer must contain.** The composition document, so the version can be reopened, plus every
blob the composition needs to be self-sufficient: each block's envelope, and whatever content those
blocks name but do not carry -- the observed bytes and normalized view of a canonical block, and the
same for any other schema that keeps its datum in the store. A layer without them would arrive as a
set of pointers the consumer cannot follow.

The blocks are asked what they name rather than tested for their type, so a schema that starts naming
content is packed correctly without touching this module.

**Why the packing is deterministic.** A layer is content-addressed, so two clients packing the same
module must produce the same digest, or push deduplication silently stops working and every push
re-uploads everything. ``tarfile`` and ``gzip`` both embed timestamps and ownership by default, so
every one of those fields is pinned here and entries are written in sorted order.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from collections.abc import Iterable

from boltzmann.exceptions import DistributionError
from boltzmann.identity.digest import BlockId, Digest, OciDigest
from boltzmann.module.composition import Composition
from boltzmann.module.module import Module
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.base import BlockStore

COMPOSITION_ENTRY = "composition.json"

SNAPSHOT_PREFIX = "snapshots/"
"""Prefix of a history layer entry, one per snapshot document."""
"""Name of the composition document inside a layer."""

BLOB_PREFIX = "blobs/"
"""Directory inside a layer holding content-addressed blobs, named by hex digest."""

GZIP_LEVEL = 9
"""Compression level. Fixed, because it is part of what makes the layer digest reproducible."""

INFLATE_CHUNK = 1 << 20
"""How much of a layer is decompressed per read while the running total is checked."""

MAX_EXPANSION_RATIO = 100
"""How far a layer may expand relative to its compressed size.

A descriptor states the size of the *compressed* blob, so it says nothing directly about what
decompressing costs -- which is why the bound is a ratio. A layer is a tar of block envelopes and
observed bytes: the JSON compresses well and the sources usually do not, so a real brain lands far
below this. A gzip bomb does not, and cannot: to exceed the bound it has to grow the blob the
consumer already agreed to download.
"""

MIN_INFLATE_ALLOWANCE = 8 << 20
"""Floor on the ceiling, so a tiny layer is never refused for being efficiently compressed.

A module of a few blocks compresses to almost nothing, and a strict ratio against that would reject
it. Eight megabytes is small enough to be harmless and large enough that no legitimate small layer
meets it.
"""

DEFAULT_MAX_INFLATED = 4 << 30
"""Absolute backstop, whatever the ratio permits.

A layer that decompresses to more than this is beyond what unpacking into memory can serve, hostile
or not.
"""


def required_blobs(module: Module) -> list[Digest]:
    """
    Every blob a layer must carry for this module to be self-sufficient.

    Args:
        module (Module): The module version to pack.

    Returns:
        list[Digest]: The block envelopes, plus the content the blocks name but do not carry. Sorted, so
        the answer does not depend on iteration order.
    """
    needed: list[Digest] = list(module.composition.block_ids)

    # Asked of every module, not only canonical: a block of any type may name its content, and content
    # this misses is a published layer whose pointers lead nowhere.
    for block_id in module.composition.block_ids:
        if not module.store.is_resolvable(block_id):
            continue
        needed.extend(module.get(block_id).content_digests)

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


def pack_history(documents: Iterable[bytes]) -> bytes:
    """
    Pack a set of snapshot documents into the history layer.

    Order is by digest rather than by chain position, so two clients publishing the same history produce
    byte-identical layers and the registry deduplicates them -- the same reason a composition's leaves are
    sorted. The chain is not lost by sorting it: each document names its own parents.

    Args:
        documents (Iterable[bytes]): The snapshot documents, as stored.

    Returns:
        bytes: The gzipped tar.
    """
    payloads = {OciDigest.of(document): document for document in documents}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for digest in sorted(payloads, key=lambda value: value.hex):
            _add(archive, f"{SNAPSHOT_PREFIX}{digest.hex}", payloads[digest])

    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", compresslevel=GZIP_LEVEL, mtime=0) as stream:
        stream.write(raw.getvalue())
    return compressed.getvalue()


def unpack_history(data: bytes, store: BlockStore, max_size: int | None = None) -> list[OciDigest]:
    """
    Unpack a history layer into a store.

    Every document is written through the store's content-addressed put and parsed as a snapshot, so a
    layer whose bytes were tampered with cannot land under the digest a lineage names, and one carrying
    something that is not a snapshot is refused rather than filed away to fail later.

    The entry name is checked against the content it holds even though content addressing makes it
    redundant -- the same reason a stored composition is checked against the root it is filed under. A
    producer whose naming and payloads disagree is malformed, and catching that at the boundary is the
    difference between one clear refusal here and an unexplained "no common ancestor" much later.

    Args:
        data (bytes): The layer blob.
        store (BlockStore): Where to write the documents.
        max_size (int | None): Most bytes the layer may decompress to. Defaults to the ratio bound.

    Returns:
        list[OciDigest]: The snapshots now resolvable, in the order the layer carried them.

    Raises:
        DistributionError: If the layer is malformed, expands past the bound, carries an entry that is not
            a snapshot document, or names an entry something other than the digest of its own content.
    """
    limit = max_size if max_size is not None else len(data) * MAX_EXPANSION_RATIO
    written = []
    with tarfile.open(fileobj=io.BytesIO(_inflate(data, limit)), mode="r:") as archive:
        for info in archive.getmembers():
            if not info.isfile() or not info.name.startswith(SNAPSHOT_PREFIX):
                continue
            handle = archive.extractfile(info)
            if handle is None:
                continue
            payload = handle.read()
            try:
                Snapshot.model_validate_json(payload)
            except ValueError as error:
                raise DistributionError(
                    f"history layer entry {info.name} is not a snapshot document: {error}"
                ) from error
            digest = OciDigest.of(payload)
            if info.name.removeprefix(SNAPSHOT_PREFIX) != digest.hex:
                raise DistributionError(
                    f"history layer entry {info.name} holds the document for {digest.short} instead; the "
                    f"layer's naming and its payloads disagree, so it was not produced by a conforming push"
                )
            written.append(store.put_bytes(payload))
    return written


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


def _inflate(data: bytes, limit: int) -> bytes:
    """
    Decompress a layer, refusing to exceed ``limit`` bytes.

    A layer arrives from a registry, so its expansion ratio is chosen by whoever published it.
    Reading the stream to exhaustion let a small blob cost the consumer arbitrary memory --
    measured at over a thousandfold on an input built for it. Reading in chunks against a ceiling
    costs nothing on a well-formed layer and turns the hostile case into a refusal.

    Args:
        data (bytes): The compressed layer.
        limit (int): Most bytes the decompressed layer may occupy.

    Returns:
        bytes: The decompressed tar.

    Raises:
        DistributionError: If the layer is not readable gzip, or expands past ``limit``.
    """
    inflated = io.BytesIO()
    seen = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream:
            while chunk := stream.read(INFLATE_CHUNK):
                seen += len(chunk)
                if seen > limit:
                    raise DistributionError(
                        f"layer expands to more than {limit} bytes from {len(data)} compressed; refusing to "
                        f"decompress further. A layer's expansion is chosen by its publisher, so an "
                        f"unbounded read is an unbounded cost to whoever pulls it."
                    )
                inflated.write(chunk)
    except (OSError, EOFError) as error:
        # EOFError is what a truncated gzip stream raises, and it is not an OSError.
        raise DistributionError(f"layer is not a readable gzip stream: {error}") from error
    return inflated.getvalue()


def unpack_layer(data: bytes, store: BlockStore, max_size: int | None = None) -> Composition:
    """
    Unpack a layer into a store and recover the composition it carries.

    Every blob is written through the store's content-addressed put, so a layer whose bytes were
    tampered with cannot land under the digest it claims: the digest is recomputed from the bytes.
    The recovered composition is then checked against the blobs actually present.

    **The expansion is bounded.** These bytes came from a registry, and the compression ratio is
    whoever published them's choice, not this client's. By default the layer may expand to
    :data:`MAX_EXPANSION_RATIO` times its own compressed size -- so the only way to make unpacking
    expensive is to make the download expensive first, which the consumer already saw and agreed
    to. A caller that knows the real bound may state it as ``max_size``.

    Args:
        data (bytes): The layer blob.
        store (BlockStore): Where to write the unpacked blobs.
        max_size (int | None): Most bytes the layer may decompress to. Defaults to the ratio bound.

    Returns:
        Composition: The composition the layer carries.

    Raises:
        DistributionError: If the layer is malformed, expands past the bound, lacks its
            composition document, or names a block it did not carry.
    """
    allowed = max(len(data) * MAX_EXPANSION_RATIO, MIN_INFLATE_ALLOWANCE) if max_size is None else max_size
    limit = min(allowed, DEFAULT_MAX_INFLATED)

    entries: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(_inflate(data, limit)), mode="r") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is not None:
                    entries[member.name] = extracted.read()
    except (OSError, tarfile.TarError) as error:
        raise DistributionError(f"layer is not a readable tar: {error}") from error

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
