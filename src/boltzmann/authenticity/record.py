"""Signature records: how detached signatures persist, accumulate, and travel (paper Section 8.3).

A record is a small JSON document naming what it covers, under which namespace, by which key, and
what the signer claims. It lives *beside* the snapshot, never inside it -- the trust root is
already within the signed bytes, so a signature stored in the snapshot would be signing itself --
and it never changes the snapshot's identity: that is what lets several signatures cover one
snapshot, which is the only way to express a quorum.

Locally, each record is a content-addressed blob, and one mutable pointer indexes them by the
snapshot they cover. The blobs are content, the pointer is state -- the same split the head
pointer already makes, and the reason adding a countersignature is a pointer update rather than a
rewrite of anything.

**Cross-field checks live in the verifier, not here.** A record whose fingerprint disagrees with
its embedded key MUST be rejected (paper Section 8.3), but a pydantic validator would collapse
eight distinguishable failures into one ``ValidationError`` -- exactly what the failure table
forbids. The model checks shape; the verifier produces typed findings.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.authenticity.keys import FINGERPRINT_PATTERN, SshPublicKey
from boltzmann.authenticity.scopes import Scope
from boltzmann.authenticity.sshsig import MAX_ARMORED_LENGTH, SshSignature
from boltzmann.constants import EMPTY_CONFIG_DIGEST, PROTOCOL_VERSION, SNAPSHOT_NAMESPACE
from boltzmann.exceptions import SerializationError, SignatureFormatError
from boltzmann.identity.digest import OciDigest
from boltzmann.identity.serialization import canonicalize, parse_json_strict
from boltzmann.store.base import BlockStore

SIGNATURES_POINTER = "signatures"
"""The mutable pointer indexing signature record blobs by the snapshot they cover."""

MAX_RECORDS_PER_SNAPSHOT = 512
"""Most records one snapshot may accumulate locally.

Quorum evaluation is records times trust-root keys and both come from the artifact, so the
product must be bounded before an attacker chooses it. Far above any real quorum.
"""


class SignatureRecord(BaseModel):
    """
    One detached signature over one snapshot.

    Attributes:
        boltzmann (int): Protocol version. A record claiming a later one is refused rather than
            read, because reading it would mean applying rules this client does not implement to
            a decision about authorship.
        snapshot (OciDigest): The snapshot document the signature covers.
        namespace (str): What the signature was made under. Anything but the protocol's is
            rejected by the verifier -- a genuine signature for something else.
        key (str): The signing key's SSH fingerprint. **An index, not an authority**: SSHSIG
            carries the public key inside the blob, so verification derives the key from
            ``signature`` and uses this field only to find a record without decoding all of them
            -- and to reject the record when the two disagree.
        scopes (tuple[Scope, ...]): What the signer claims. A statement of intent that aids
            diagnosis, never the basis of a decision: the required set is computed from the
            snapshot's difference against its first parent.
        signature (str): The armored SSHSIG container. Armored rather than raw base64 so that an
            operator with no Boltzmann tooling can check it with ``ssh-keygen -Y verify``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = Field(default=PROTOCOL_VERSION, ge=1, le=PROTOCOL_VERSION)
    snapshot: OciDigest
    namespace: str = SNAPSHOT_NAMESPACE
    key: str = Field(pattern=FINGERPRINT_PATTERN.pattern)
    scopes: tuple[Scope, ...] = ()
    signature: str = Field(min_length=1, max_length=MAX_ARMORED_LENGTH)

    def canonical_bytes(self) -> bytes:
        """
        The record as canonical bytes, which is what gets stored and published.

        Returns:
            bytes: The canonically serialized record.
        """
        return canonicalize(self.model_dump(mode="json", exclude_none=True))

    @classmethod
    def from_document(cls, data: bytes) -> SignatureRecord:
        """Decode the exact canonical record bytes used as its content address."""
        try:
            document = parse_json_strict(data)
        except SerializationError as error:
            raise SignatureFormatError(f"signature record {error}") from error
        record = cls.model_validate(document)
        if record.canonical_bytes() != data:
            raise SignatureFormatError("signature record is not in canonical jcs/1 form")
        return record

    @property
    def digest(self) -> OciDigest:
        """The record blob's content address.

        Never an identity for the *signature*: the armor wrap and the reserved field are producer
        choices, so two records can differ in bytes while carrying one signature. Deduplication
        of signers happens on the embedded key, in the verifier.
        """
        return OciDigest.of(self.canonical_bytes())

    @property
    def parsed(self) -> SshSignature:
        """
        The armored signature, decoded.

        Returns:
            SshSignature: The parsed structure.

        Raises:
            SignatureFormatError: If the armor or the blob inside it cannot be read.
        """
        return SshSignature.parse(self.signature)

    @property
    def embedded_key(self) -> SshPublicKey | None:
        """The key inside the signature blob, or ``None`` when the armor does not parse."""
        try:
            return self.parsed.public_key
        except SignatureFormatError:
            return None


class SignatureIndex(BaseModel):
    """
    The mutable index: which record blobs cover which snapshot.

    Attributes:
        boltzmann (int): Protocol version that wrote this pointer.
        entries (dict[str, list[OciDigest]]): Record blob digests by covered snapshot digest.
            Keys are the string form because JSON object keys are strings; values are kept
            sorted by hex so two stores holding the same records write identical pointers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = PROTOCOL_VERSION
    entries: dict[str, list[OciDigest]] = Field(default_factory=dict)


def read_index(store: BlockStore) -> SignatureIndex:
    """
    Read the signature index, empty when none was ever written.

    Args:
        store (BlockStore): The store holding the pointer.

    Returns:
        SignatureIndex: The index.
    """
    raw = store.read_pointer(SIGNATURES_POINTER)
    return SignatureIndex.model_validate(parse_json_strict(raw)) if raw else SignatureIndex()


def write_index(store: BlockStore, index: SignatureIndex) -> None:
    """
    Persist the signature index.

    Args:
        store (BlockStore): The store holding the pointer.
        index (SignatureIndex): The index to write.
    """
    store.write_pointer(SIGNATURES_POINTER, canonicalize(index.model_dump(mode="json", exclude_none=True)))


def store_record(store: BlockStore, record: SignatureRecord) -> OciDigest:
    """
    Persist a signature record: blob first, pointer second.

    Storing the same record twice is a no-op, because both the blob and its index entry are
    keyed by content. The snapshot's own identity never moves -- which is the entire point of
    detaching signatures.

    Args:
        store (BlockStore): Where the blob and the index live.
        record (SignatureRecord): The record to keep.

    Returns:
        OciDigest: The record blob's content address.

    Raises:
        SignatureFormatError: If this snapshot already holds :data:`MAX_RECORDS_PER_SNAPSHOT`
            records. Quorum evaluation must stay bounded by something the attacker does not pick.
    """
    index = read_index(store)
    held = list(index.entries.get(str(record.snapshot), []))
    digest = store.put_bytes(record.canonical_bytes())
    if digest in held:
        return digest
    if len(held) >= MAX_RECORDS_PER_SNAPSHOT:
        raise SignatureFormatError(
            f"snapshot {record.snapshot.short} already holds {MAX_RECORDS_PER_SNAPSHOT} signature "
            f"records; no legitimate quorum needs more, and an unbounded pile is a verification bill "
            f"someone else pays"
        )
    held.append(digest)
    entries = {**index.entries, str(record.snapshot): sorted(held, key=lambda value: value.hex)}
    write_index(store, SignatureIndex(boltzmann=index.boltzmann, entries=entries))
    return digest


def for_snapshot(store: BlockStore, snapshot: OciDigest) -> list[SignatureRecord]:
    """
    Every record held over one snapshot, in blob-digest order.

    Args:
        store (BlockStore): Where the blobs and the index live.
        snapshot (OciDigest): The covered snapshot.

    Returns:
        list[SignatureRecord]: The records, possibly empty.
    """
    index = read_index(store)
    return [
        SignatureRecord.from_document(store.get_bytes(digest))
        for digest in index.entries.get(str(snapshot), [])
        if store.is_resolvable(digest)
    ]


def reachable_signatures(store: BlockStore, snapshots: Iterable[str]) -> set[str]:
    """
    The record blob hexes that must survive a prune.

    A signature blob is named by the index pointer, not by any snapshot or tag, so without this
    a prune would reclaim it -- and a signature a garbage collection can remove is not a
    signature. Records covering snapshots that are themselves gone are not kept: a signature
    over an unresolvable document attests nothing.

    Args:
        store (BlockStore): The store holding the index.
        snapshots (Iterable[str]): Hex digests of every snapshot the prune keeps.

    Returns:
        set[str]: Hex digests of the record blobs to keep.
    """
    kept = set(snapshots)
    index = read_index(store)
    keep = {
        digest.hex
        for snapshot, digests in index.entries.items()
        if OciDigest.parse(snapshot).hex in kept
        for digest in digests
    }
    if keep:
        # The OCI empty blob is the config of every signature manifest; two bytes that, if
        # reclaimed, leave every published signature manifest pointing at nothing.
        keep.add(OciDigest.parse(EMPTY_CONFIG_DIGEST).hex)
    return keep
