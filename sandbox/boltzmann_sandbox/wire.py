"""SDK results as JSON an MCP client can read.

Almost nothing here is conversion. The SDK's results are pydantic models whose digests already serialize
as ``"sha256:…"``, so ``model_dump(mode="json")`` is the whole job. What this module adds is the part
``model_dump`` cannot see: the computed properties. ``Snapshot.digest``, ``EvidenceBundle.all_verified``
and ``CascadePlan.size`` are derived rather than stored, and they are exactly the values a caller wants
first -- which version is this, did everything verify, how much would this drop take with it.

Every function is deliberately dumb. A tool that reshapes a result is a tool whose output has to be
trusted separately from the SDK's; keeping the shape flat and adding only derived values means the client
sees what the protocol produced.
"""

from __future__ import annotations

from typing import Any

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.distribution.manifest import BrainManifest
from boltzmann.identity.digest import MerkleRoot
from boltzmann.ingest.commit import CommitResult
from boltzmann.ingest.register import RegistrationResult
from boltzmann.ingest.validation import ValidationReport
from boltzmann.merkle.proof import InclusionProof
from boltzmann.module.snapshot import Snapshot
from boltzmann.query.evidence import EvidenceBundle
from boltzmann.retention.requests import (
    CascadePlan,
    DropResult,
    PruneReport,
    RedactionResult,
    ResolvabilityReport,
    SupersessionResult,
)
from pydantic import BaseModel


def snapshot(value: Snapshot) -> dict[str, Any]:
    """
    A version, with the digest that names it.

    Args:
        value (Snapshot): The snapshot.

    Returns:
        dict[str, Any]: Its fields, plus ``digest`` -- which is computed from the document rather than
        stored in it, because a snapshot cannot contain its own hash.
    """
    return {"digest": str(value.digest), **value.model_dump(mode="json")}


def _versioned(result: BaseModel, version: Snapshot) -> dict[str, Any]:
    """
    A write's result, with its snapshot reduced to the digest that names it.

    Every write returns the whole new snapshot document. Sending it back inside every result would repeat
    the module list on each call and bury the one value a caller acts on -- which version this produced.
    The document is one ``brain_state`` away.

    Args:
        result (BaseModel): The result of a write.
        version (Snapshot): The snapshot it produced.

    Returns:
        dict[str, Any]: The result, with ``snapshot`` as a digest string.
    """
    return {**result.model_dump(mode="json"), "snapshot": str(version.digest)}


def registration(result: RegistrationResult) -> dict[str, Any]:
    """
    What registering a source produced.

    Args:
        result (RegistrationResult): The registration.

    Returns:
        dict[str, Any]: The block's identity, whether the bytes were already held, and the version this
        created -- ``None`` when ``duplicate`` is true, because re-registering identical bytes is a no-op
        and a no-op does not mint a version.
    """
    return {
        "block_id": str(result.block_id),
        "duplicate": result.duplicate,
        "snapshot": str(result.commit.snapshot.digest) if result.commit is not None else None,
    }


def commit(result: CommitResult) -> dict[str, Any]:
    """
    What committing validated candidates produced.

    Args:
        result (CommitResult): The commit.

    Returns:
        dict[str, Any]: The blocks committed, the provenance written, the new roots, and the version.
    """
    return _versioned(result, result.snapshot)


def dropped(result: DropResult) -> dict[str, Any]:
    """
    What a drop removed.

    Args:
        result (DropResult): The drop.

    Returns:
        dict[str, Any]: What was removed per module, the rebuilt roots, the provenance recording the
        removal, and the version. ``review_required`` is true when something that cited the removed
        evidence now needs a human decision.
    """
    return _versioned(result, result.snapshot)


def supersession(result: SupersessionResult) -> dict[str, Any]:
    """
    What a supersession or demotion recorded.

    Nothing was removed: the block stays in the composition and keeps proving into the root. What changed
    is accessibility (paper Section 10.4).

    Args:
        result (SupersessionResult): The result.

    Returns:
        dict[str, Any]: The provenance written and the version.
    """
    return _versioned(result, result.snapshot)


def redaction(result: RedactionResult) -> dict[str, Any]:
    """
    What a redaction did, and what it cost.

    Args:
        result (RedactionResult): The redaction.

    Returns:
        dict[str, Any]: The mechanism used, what it applied to, and whether prior roots still verify --
        the one operation in the protocol that can invalidate them.
    """
    return _versioned(result, result.snapshot)


def prune(report: PruneReport) -> dict[str, Any]:
    """
    What pruning reclaimed, or would.

    Args:
        report (PruneReport): The report.

    Returns:
        dict[str, Any]: The counts, what was reclaimed, and whether this was a dry run.
    """
    return {**report.model_dump(mode="json"), "reclaimed_count": report.reclaimed_count}


def resolvability(report: ResolvabilityReport) -> dict[str, Any]:
    """
    Which blocks can be read, and which are named but absent.

    A block can be a verifiable member of a version and still not be readable -- after a selective
    install, or a redaction. The distinction matters: membership proves, resolution reads.

    Args:
        report (ResolvabilityReport): The report.

    Returns:
        dict[str, Any]: Counts per module, and whether everything named is readable.
    """
    payload = report.model_dump(mode="json")
    return {
        **payload,
        "counts": {
            state: {kind: len(blocks) for kind, blocks in payload[state].items()}
            for state in ("resolvable", "tombstoned", "missing")
        },
        "intact": report.is_intact,
    }


def evidence(bundle: EvidenceBundle) -> dict[str, Any]:
    """
    An Evidence Bundle, with the verification summary spelled out.

    Args:
        bundle (EvidenceBundle): What a query returned.

    Returns:
        dict[str, Any]: Its fields, plus ``all_verified``. Data and provenance, never prose: there is no
        answer field to fill in, by design (paper Section 9.3).
    """
    return {**bundle.model_dump(mode="json"), "all_verified": bundle.all_verified}


def validation(report: ValidationReport) -> dict[str, Any]:
    """
    A validation report, summarized by status.

    The per-candidate detail stays, because a rejection is only useful with its code and its issue. The
    counts are what a model reads first to know whether to try again.

    Args:
        report (ValidationReport): What the gate decided.

    Returns:
        dict[str, Any]: The report, plus a count per status and ``is_clean``.
    """
    counts: dict[str, int] = {}
    for result in report.results:
        counts[result.status.value] = counts.get(result.status.value, 0) + 1

    return {
        **report.model_dump(mode="json"),
        "counts": counts,
        "committable": len(report.committable),
        "is_clean": report.is_clean,
    }


def cascade(plan: CascadePlan) -> dict[str, Any]:
    """
    A drop plan, with its size.

    Args:
        plan (CascadePlan): What a drop would do.

    Returns:
        dict[str, Any]: The plan, plus ``size``. A privileged plan is one whose origin is canonical
        evidence, which always cascades to whatever cited it (paper Section 10.3).
    """
    return {**plan.model_dump(mode="json"), "size": plan.size}


def proof(value: InclusionProof, root: MerkleRoot) -> dict[str, Any]:
    """
    An inclusion proof, already checked.

    Returning the proof without checking it would make the caller responsible for verification, which is
    the one thing the protocol does not leave to a caller.

    Args:
        value (InclusionProof): The audit path.
        root (MerkleRoot): The root it should verify against.

    Returns:
        dict[str, Any]: The proof, the root, and whether it verifies.
    """
    return {**value.model_dump(mode="json"), "root": str(root), "verified": value.verify(root)}


def block(value: Block, memory_type: MemoryType) -> dict[str, Any]:
    """
    A resolved block.

    Args:
        value (Block): The block, already verified by hash on the way out of the store.
        memory_type (MemoryType): Which module holds it.

    Returns:
        dict[str, Any]: Its identity, kind, and payload.
    """
    return {
        "block_id": str(value.block_id),
        "memory_type": memory_type.value,
        "schema_version": value.SCHEMA_VERSION,
        "payload": value.payload(),
    }


def manifest(value: BrainManifest) -> dict[str, Any]:
    """
    A brain manifest, with its digest.

    Args:
        value (BrainManifest): The manifest.

    Returns:
        dict[str, Any]: The OCI descriptor set, plus the digest the manifest bytes hash to -- which is
        what a registry files it under and what ``push`` returns.
    """
    from boltzmann.identity.digest import OciDigest

    payload = value.model_dump(mode="json")
    return {"digest": str(OciDigest.of(value.to_bytes())), **payload}
