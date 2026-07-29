"""The whole lifecycle, end to end, against a real registry.

This is the run that answers the questions the SDK's own suite cannot. It registers a source, derives
knowledge from it, searches, proves membership, corrects a mistake, removes evidence and watches the
cascade, reclaims storage, publishes, and installs into a second empty brain -- then checks that the
installed version is byte-for-byte the one that was published, and that the travelling index arrived with
the model tag that produced it.

Every step asserts. A demo that prints without checking is a screenshot: it looks like evidence and
proves nothing. So each phase states what has to hold, and the process exits non-zero the moment one does
not -- which is what makes this usable as a smoke test against a registry nobody has tried yet.

The proposer is deterministic and carries no model. Ingestion needs an external model to decide what
knowledge a source yields, but that decision is not what is under test here: the transport, the identity
arithmetic and the retention cascade are. A fixed set of facts makes the run reproducible, and the digests
it prints comparable between two machines.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from typing import TYPE_CHECKING, Any, Final

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.exceptions import BoltzmannError
from boltzmann.indices.base import IndexKind
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.query.request import Query, QueryFilters, QueryHints
from boltzmann.retention.requests import DropRequest

from boltzmann_sandbox.brain import open_brain, registry_client
from boltzmann_sandbox.config import ConfigError, Settings, load
from boltzmann_sandbox.indices import VectorIndex

if TYPE_CHECKING:
    from boltzmann.brain import Brain
    from boltzmann.indices.base import Index
    from boltzmann.ingest.task import ProcessingTask

FACTS: Final = [
    ("formula", "Fourier series", "decomposes a periodic function into a sum of sines and cosines", "p.1"),
    ("concept", "Laplace transform", "maps a function of time into a function of complex frequency", "p.2"),
    ("fact", "Nyquist rate", "sampling must exceed twice the highest frequency present", "p.3"),
]
"""What the source supports, as ``(kind, label, statement, locator)``.

Each statement appears verbatim in :data:`LECTURE` below. That is not cosmetic: :func:`proposer` only
proposes what it can find in the bytes, so a statement that drifted from the source would be silently
dropped rather than committed -- which is the correct behaviour and a confusing way to discover a typo.
"""

LECTURE: Final = (
    b"%PDF-1.7 Lecture 07 -- Fourier analysis.\n"
    b"A Fourier series decomposes a periodic function into a sum of sines and cosines.\n"
    b"The Laplace transform maps a function of time into a function of complex frequency.\n"
    b"For the Nyquist rate, sampling must exceed twice the highest frequency present.\n"
)

CORRECTED: Final = (
    LECTURE.replace(
        b"Lecture 07 -- Fourier analysis.",
        b"Lecture 07 -- Fourier analysis (corrected).",
    )
    + b"Aliasing is what sampling below the Nyquist rate produces.\n"
)
"""The same lecture with an added line, to supersede the first without changing what it supports."""


class DemoError(Exception):
    """An assertion the lifecycle has to satisfy, and did not."""


def require(condition: bool, message: str) -> None:
    """
    Assert, with an explanation that survives ``-O``.

    A plain ``assert`` disappears under optimization, and this file is the one place where a silently
    skipped check would turn a failing run into a passing one.

    Args:
        condition (bool): What must hold.
        message (str): What it means when it does not.

    Raises:
        DemoError: If the condition is false.
    """
    if not condition:
        raise DemoError(message)


def as_vector(index: Index) -> VectorIndex:
    """
    The vector index as this sandbox's engine.

    ``open_index`` hands back the interface, because which engine sits behind an index is the
    implementation's choice. Reading its vectors means first establishing that it is the engine this client
    registered -- which is also worth checking: an index that came back as something else would mean the
    layer was loaded into the wrong place.

    Args:
        index (Index): What the brain returned.

    Returns:
        VectorIndex: The same object, narrowed.

    Raises:
        DemoError: If it is a different engine.
    """
    if not isinstance(index, VectorIndex):
        raise DemoError(f"expected the sandbox's {VectorIndex.__name__}, got {type(index).__name__}")
    return index


def step(title: str) -> None:
    """Announce a phase.

    Flushed, because this doubles as a smoke test against registries nobody has tried: piped into a log,
    block buffering would hold every line until the process exits, and a run that hangs would show nothing
    at all rather than the step it hung on.
    """
    print(f"\n\033[1m{title}\033[0m", flush=True)


def note(label: str, value: object) -> None:
    """Report one value under a phase."""
    print(f"  {label:26s} {value}", flush=True)


def proposer(task: ProcessingTask, source: bytes) -> CandidateSet:
    """
    A deterministic stand-in for the external model.

    It proposes only the facts whose statement actually appears in the bytes it was handed. That is the
    one thing a real proposer must get right -- a candidate the source does not support is a candidate the
    validation gate will reject as unjustified -- and doing it here means the run is reproducible without
    being a fiction: hand it a different source and it proposes less.

    Args:
        task (ProcessingTask): What the brain asked for. Its ``source`` is what the candidates must cite.
        source (bytes): The registered bytes, which a real model would read.

    Returns:
        CandidateSet: One semantic candidate per fact the source supports.
    """
    text = source.decode("utf-8", errors="replace").casefold()
    return CandidateSet(
        producer=Producer(kind=ProducerKind.MODEL, id="sandbox-fixed-proposer", version="1"),
        candidates=[
            Candidate(
                memory_type=MemoryType.SEMANTIC,
                evidence=[task.source],
                locator=locator,
                payload={"kind": kind, "label": label, "statement": statement, "subject": "signals"},
            )
            for kind, label, statement, locator in FACTS
            if statement.casefold() in text
        ],
    )


def ingest(brain: Brain, settings: Settings, data: bytes) -> Any:
    """Register a source and derive knowledge from it, in one commit path."""
    request = RegistrationRequest(
        media_type="application/pdf",
        actor=Actor(id=settings.actor, kind=ActorKind.HUMAN),
        license="CC-BY-4.0",
    )
    return brain.ingest(data, request, proposer)


def roots(brain: Brain) -> dict[str, str]:
    """Every module's Merkle root, keyed by memory type, for comparing two brains."""
    return {kind.value: str(reference.root) for kind, reference in brain.snapshot().modules.items()}


async def run(settings: Settings) -> None:
    """
    The lifecycle.

    Args:
        settings (Settings): Validated configuration.

    Raises:
        DemoError: If any step does not hold.
    """
    # A fresh publisher every run, so the digests printed are a function of the fixed inputs and nothing
    # else. Reusing a directory would make the version chain depend on how many times this was run.
    publisher_path = settings.brain_path
    consumer_path = settings.brain_path.parent / f"{settings.brain_path.name}-consumer"
    for path in (publisher_path, consumer_path):
        shutil.rmtree(path, ignore_errors=True)

    client = registry_client(settings)
    brain = open_brain(settings, publisher_path)

    step("1. Register and ingest -- one source, three facts")
    commit = ingest(brain, settings, LECTURE)
    source = brain.module(MemoryType.CANONICAL).block_ids[0]
    note("canonical evidence", source.short)
    note("committed", f"{len(commit.committed)} blocks")
    note("version", brain.snapshot().digest.short)
    require(len(commit.committed) == len(FACTS), f"expected {len(FACTS)} blocks, got {len(commit.committed)}")

    step("2. Re-register the same bytes -- dedup is a no-op")
    duplicate = brain.register(
        LECTURE,
        RegistrationRequest(media_type="application/pdf", actor=Actor(id=settings.actor, kind=ActorKind.HUMAN)),
    )
    note("same identity", duplicate.block_id == source)
    note("duplicate", duplicate.duplicate)
    require(duplicate.duplicate and duplicate.block_id == source, "re-registering identical bytes minted a new block")

    step("3. Search -- hybrid ranking, every match verified")
    bundle = brain.search(Query(text="periodic function sines", hints=QueryHints(limit=5)))
    for match in bundle.matches:
        note(f"  {match.score}", f"{match.content.get('label')}  <- {match.sources[0].locator}")
    require(bool(bundle.matches), "the search found nothing")
    require(bundle.all_verified, "a match came back unverified")
    require(bundle.matches[0].content.get("label") == "Fourier series", "the strongest match is not the right one")

    step("4. Prove membership -- O(log n), without the rest of the module")
    best = bundle.matches[0].block_id
    proof = brain.prove(best, MemoryType.SEMANTIC)
    note("block", best.short)
    note("audit path", f"{len(proof.audit_path)} hashes for a tree of {proof.tree_size}")
    note("verifies", proof.verify(brain.root_of(MemoryType.SEMANTIC)))
    require(proof.verify(brain.root_of(MemoryType.SEMANTIC)), "an inclusion proof did not verify")
    require(brain.verify(), "the brain did not verify")

    step("5. The travelling index -- what no consumer can rebuild")
    published_index = as_vector(brain.open_index(MemoryType.SEMANTIC, IndexKind.VECTOR))
    inverted = brain.open_index(MemoryType.SEMANTIC, IndexKind.INVERTED)
    note("vector model", published_index.model_tag)
    note("rebuildable", f"vector={published_index.rebuildable}  inverted={inverted.rebuildable}")
    note("dump size", f"{len(published_index.dump())} bytes for {len(published_index.vectors)} vectors")
    require(not published_index.rebuildable, "the vector index claims to be rebuildable")
    require(inverted.rebuildable, "the inverted index claims it must travel")

    step("6. Replace the source -- a correction, not a rewrite")
    replacement = brain.replace(
        CORRECTED,
        RegistrationRequest(media_type="application/pdf", actor=Actor(id=settings.actor, kind=ActorKind.HUMAN)),
        supersedes=source,
    )
    note("new evidence", replacement.block_id.short)
    note("supersedes", source.short)
    superseded = brain.search(
        Query(text="", filters=QueryFilters(memory_types=[MemoryType.CANONICAL], include_superseded=True))
    )
    note("both still members", f"{len(superseded.matches)} canonical blocks")
    require(len(superseded.matches) == 2, "the superseded source left the composition instead of being demoted")
    require(brain.verify(), "the brain stopped verifying after a supersession")

    step("7. Publish")
    before = roots(brain)
    published = brain.snapshot().digest
    manifest = brain.pack(tag=settings.tag)
    note("layers", f"{len(manifest.layers)} + config")
    for layer in manifest.layers:
        kind = layer.annotations.get("ai.gaussia.boltzmann.memory-type", "index")
        note(f"  {kind}", f"{layer.digest.short}  {layer.size:>7} bytes  {layer.media_type.split('.')[-1]}")

    try:
        pushed = await brain.push(client, settings.registry, settings.tag)
    except BoltzmannError as error:
        raise DemoError(
            f"the registry refused the artifact: {error}\n"
            f"  This is the finding this demo exists to produce. A registry that rejects a manifest whose "
            f"artifactType is application/vnd.gaussia.boltzmann.brain.v1+json does not support OCI "
            f"artifacts the way the protocol needs. Record the status and the body in the README."
        ) from error
    note("manifest", pushed.short)
    note("version", published.short)

    step("8. Install into an empty brain")
    consumer = open_brain(settings, consumer_path)
    plan = await consumer.plan_pull(client, settings.registry, settings.tag)
    note("modules", ", ".join(kind.value for kind in plan.modules))
    note("layers to fetch", f"{len(plan.fetch_layers)}, reusing {len(plan.reuse_layers)} already held by digest")
    note("indices to rebuild", ", ".join(plan.rebuild_indices) or "none")
    note("indices that travel", ", ".join(kind.value for kind in plan.fetch_vector_indices) or "none")
    installed = await consumer.pull(client, settings.registry, settings.tag)
    note("installed version", installed.digest.short)

    note("same digest", installed.digest == published)
    require(
        installed.digest == published,
        f"the installed version is {installed.digest.short}, not the published {published.short} -- "
        f"a round trip that changes the digest means the artifact is not the version",
    )
    require(roots(consumer) == before, "a module's root changed in transit")
    require(consumer.verify(), "the installed brain did not verify")

    step("9. The index arrived, not rebuilt")
    landed = as_vector(consumer.open_index(MemoryType.SEMANTIC, IndexKind.VECTOR))
    note("vectors", len(landed.vectors))
    note("model tag", landed.model_tag)
    note("identical dump", landed.dump() == published_index.dump())
    require(
        landed.dump() == published_index.dump(),
        "the travelling index did not survive the round trip byte for byte",
    )

    step("10. Search the installed brain")
    remote = consumer.search(Query(text="complex frequency", hints=QueryHints(limit=3)))
    for match in remote.matches:
        note(f"  {match.score}", match.content.get("label"))
    require(remote.all_verified, "a match from the installed brain came back unverified")
    require(bool(remote.matches), "the installed brain found nothing")

    step("11. Drop the evidence -- the privileged cascade")
    # The original, not the replacement: it is the block the semantic knowledge cites, so dropping it is
    # what makes the cascade visible. Dropping the replacement instead would remove one block and nothing
    # else, which demonstrates the machinery without demonstrating the point.
    request = DropRequest(
        blocks=[source],
        memory_type=MemoryType.CANONICAL,
        actor=Actor(id=settings.actor, kind=ActorKind.HUMAN),
        reason="demonstrating Section 10.3",
        # The wrong lecture was ingested and a corrected one replaced it. Naming the replacement is what
        # separates "this knowledge lost its justification" from "this knowledge can be justified again
        # from the right source" -- the difference between a deletion and a re-derivation.
        rederive_against=replacement.block_id,
    )
    cascade = brain.plan_drop(request)
    note("privileged", cascade.privileged)
    note("takes with it", f"{cascade.size} blocks")
    for kind, blocks in cascade.dependents.items():
        note(f"  {kind.value}", ", ".join(block.short for block in blocks))
    note("re-derivable", f"{len(cascade.rederivable)} against the replacement")
    require(cascade.privileged, "dropping canonical evidence was not treated as privileged")
    require(
        cascade.size == len(FACTS),
        f"the cascade takes {cascade.size} blocks, but {len(FACTS)} were derived from this evidence -- "
        f"knowledge whose source was removed can no longer be justified",
    )
    require(
        len(cascade.rederivable) == len(FACTS),
        f"only {len(cascade.rederivable)} of {len(FACTS)} blocks were marked re-derivable against a "
        f"replacement that supports the same statements",
    )

    result = brain.drop(request)
    dropped = sum(len(blocks) for blocks in result.dropped.values())
    note("dropped", f"{dropped} blocks across {len(result.dropped)} modules")
    note("recorded in provenance", f"{len(result.provenance)} records")
    note("version", result.snapshot.digest.short)
    require(len(result.provenance) > 0, "a removal went unrecorded, which the protocol does not permit")

    unresolvable = brain.resolvability()
    note("still resolvable", sum(len(blocks) for blocks in unresolvable.resolvable.values()))
    require(brain.verify(), "the brain stopped verifying after a drop")

    step("12. Older roots still verify")
    for kind, root in before.items():
        note(kind, f"{root[:20]}… retained")
    require(consumer.verify(), "the installed version stopped verifying because the publisher dropped blocks")

    step("13. Prune -- reclaim what no retained root needs")
    preview = brain.prune(dry_run=True)
    note("would reclaim", f"{preview.reclaimed_count} blobs")
    note("reachable", f"{preview.reachable} from {preview.retained_roots} retained roots")
    reclaimed = brain.prune(dry_run=False)
    note("reclaimed", f"{reclaimed.reclaimed_count} blobs")
    require(brain.verify(), "the brain stopped verifying after a prune")
    require(consumer.verify(), "pruning the publisher broke the consumer")

    print(f"\n\033[32mThe lifecycle held, against {settings.reference}.\033[0m")
    print(f"  publisher {publisher_path}\n  consumer  {consumer_path}")


def main() -> int:
    """
    Run the lifecycle against the configured registry.

    Returns:
        int: ``0`` if every step held, ``1`` otherwise.
    """
    try:
        settings = load()
    except ConfigError as error:
        print(f"cannot run: {error}", file=sys.stderr)
        return 1

    print(f"\033[1mboltzmann lifecycle\033[0m -- {settings.reference}")
    if not settings.authenticated:
        print("  anonymous: this will fail at the push unless the registry allows it")

    try:
        asyncio.run(run(settings))
    except DemoError as error:
        print(f"\n\033[31mFAILED\033[0m {error}", file=sys.stderr)
        return 1
    except BoltzmannError as error:
        print(f"\n\033[31mFAILED\033[0m the SDK refused an operation: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
