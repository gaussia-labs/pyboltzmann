"""Regressions for defects an audit of the SDK found, each stating the behaviour that was missing.

None of these were reachable from the rest of the suite, which passed throughout. They are kept
together rather than scattered into the module suites because what they have in common is how they
were found and what they cost -- the docstrings say which invariant was being violated, so a change
that reintroduces one fails against the reasoning rather than against a bare assertion.

Grouped by what broke: silent data loss first, then what untrusted registry input could do, then the
correctness gaps, then the paths that were quadratic in the size of a brain. The last group asserts
on *shape* -- that doubling the input does not quadruple the work -- rather than on absolute times,
so it does not turn into a flaky benchmark on a loaded machine.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import time
from pathlib import Path

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.blocks.semantic import SemanticBlock, SemanticKind
from boltzmann.brain import Brain
from boltzmann.distribution.layers import unpack_layer
from boltzmann.distribution.local import LocalLayoutRegistry
from boltzmann.distribution.manifest import parse_manifest
from boltzmann.exceptions import BlockIntegrityError, DistributionError, ModuleError
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.merkle.tree import MerkleTree
from boltzmann.retention.policy import PERMISSIVE_POLICY, RetentionPolicy
from boltzmann.retention.requests import ProducerDropRequest
from boltzmann.store.memory import MemoryBlockStore
from boltzmann.store.oci_layout import OciLayoutStore

ACTOR = Actor(kind=ActorKind.HUMAN, id="auditor")
REDACTING_POLICY = RetentionPolicy(redactable_media_types=["*"])


def _request(media_type: str = "text/plain") -> RegistrationRequest:
    return RegistrationRequest(actor=ACTOR, media_type=media_type)


# --- Data loss ----------------------------------------------------------------------


def test_redaction_spares_content_another_live_block_names(tmp_path: Path) -> None:
    """Redacting one block must not destroy bytes a different, un-redacted block names.

    Two canonical blocks over the same bytes are distinct blocks with one blob -- registering the
    same source under two media types is enough. ``redact`` used to tombstone every digest its target
    named, taking the survivor's evidence with it: the survivor stayed a resolvable member of the
    composition, so ``verify`` still passed while the datum it pointed at was gone. Silent evidence
    loss, on the one call whose contract is that it destroys exactly what was named.
    """
    brain = Brain.open(tmp_path, actor=ACTOR, policy=REDACTING_POLICY)
    data = b"one source, registered twice under two media types"
    first = brain.register(data, _request("text/plain"))
    second = brain.register(data, _request("text/markdown"))

    survivor = brain.module(MemoryType.CANONICAL).get(second.block_id)
    brain.redact(first.block_id, MemoryType.CANONICAL, reason="audit")

    assert brain.store.is_resolvable(survivor.blob)


def test_prune_refuses_to_run_when_a_block_envelope_is_corrupt(tmp_path: Path) -> None:
    """A block that cannot be decoded must stop the sweep, not be read as naming nothing.

    ``_bytes_named_by`` used to catch every exception and return an empty set, so a single corrupt
    envelope dropped that block's content from the marked set and the sweep reclaimed evidence a
    retained root still names -- a bit flip turning into permanent loss on the one call documented
    as reclaiming only what nothing needs.

    Refusing is the conservative direction, and it is also the informative one: an operator learns
    the store is corrupt instead of quietly losing a source. A prune that declined can be run again
    once the block is restored or explicitly redacted; a prune that ran cannot be undone.
    """
    brain = Brain.open(tmp_path, actor=ACTOR)
    registration = brain.register(b"%PDF-1.4 evidence a retained root names", _request("application/pdf"))
    block = brain.module(MemoryType.CANONICAL).get(registration.block_id)

    envelope = brain.store.blobs_dir / registration.block_id.hex
    envelope.write_bytes(envelope.read_bytes() + b" ")

    reopened = Brain.open(tmp_path, actor=ACTOR)
    with pytest.raises(BlockIntegrityError):
        reopened.prune(dry_run=False)
    assert reopened.store.is_resolvable(block.blob)


# --- Untrusted input ----------------------------------------------------------------


def test_unpack_layer_refuses_a_decompression_bomb() -> None:
    """A layer must not be decompressed unbounded into memory.

    ``pull`` hands ``unpack_layer`` bytes a registry served, and the compression ratio is the
    publisher's choice. Reading the gzip stream to exhaustion let 398 KB expand to 419 MB -- over a
    thousandfold -- for whoever pulled it. The expansion is now bounded relative to the compressed
    size the consumer already agreed to download.
    """
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as archive:
        document = json.dumps(
            {"boltzmann": 1, "memory_type": "semantic", "layout": "rfc6962-sorted/1", "block_ids": []}
        ).encode()
        info = tarfile.TarInfo("composition.json")
        info.size = len(document)
        archive.addfile(info, io.BytesIO(document))
    payload = inner.getvalue() + b"\0" * (256 * 1024 * 1024)

    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", compresslevel=9, mtime=0) as stream:
        stream.write(payload)

    with pytest.raises(DistributionError):
        unpack_layer(compressed.getvalue(), MemoryBlockStore())


def test_pull_reports_a_manifest_that_disagrees_with_its_config(tmp_path: Path) -> None:
    """A manifest advertising a module its own config blob does not name must raise DistributionError.

    ``pull`` derives the wanted modules from the manifest's layers but reads the expected root from
    the config blob. Both come from the registry and nothing forced them to agree, so an inconsistent
    artifact escaped as a bare ``KeyError`` -- unactionable, and unlike every other transport failure
    in this layer.
    """
    import asyncio

    publisher = Brain.open(tmp_path / "publisher", actor=ACTOR)
    publisher.register(b"evidence", _request())
    registry = LocalLayoutRegistry(tmp_path / "registry")
    asyncio.run(publisher.push(registry, "org/brain", "v1"))

    real = asyncio.run(registry.resolve("org/brain", "v1"))
    mislabelled = real.layers[0].model_copy(
        update={"annotations": {**real.layers[0].annotations, "ai.gaussia.boltzmann.memory-type": "semantic"}}
    )
    hostile = real.model_copy(update={"layers": [*real.layers, mislabelled]})

    class InconsistentClient:
        async def resolve(self, reference: str, tag: str):
            return hostile

        async def pull_blob(self, reference: str, digest, store) -> None:
            store.put_bytes(registry.layout(reference).get_bytes(digest))

        async def push(self, *args: object, **kwargs: object) -> None:
            raise NotImplementedError

    consumer = Brain.open(tmp_path / "consumer", actor=ACTOR)
    with pytest.raises(DistributionError):
        asyncio.run(consumer.pull(InconsistentClient(), "org/brain", "v1"))


def test_parse_manifest_rejects_a_non_object_annotations_field() -> None:
    """Every malformed manifest must surface as DistributionError, not AttributeError.

    ``parse_manifest`` called ``.get`` on whatever ``annotations`` happened to be. The value comes
    straight off the wire, so a string there crashed the parser with a type error instead of the
    documented exception. Untrusted input means untrusted *types*, not just untrusted values.
    """
    document = json.dumps(
        {
            "schemaVersion": 2,
            "artifactType": "application/vnd.gaussia.boltzmann.brain.v1+json",
            "annotations": "not an object",
            "config": {
                "mediaType": "application/vnd.gaussia.boltzmann.snapshot.v1+json",
                "digest": "sha256:" + "0" * 64,
                "size": 1,
            },
            "layers": [],
        }
    ).encode()

    with pytest.raises(DistributionError):
        parse_manifest(document)


def test_local_registry_refuses_a_reference_that_escapes_its_root(tmp_path: Path) -> None:
    """A repository reference must not be able to address a path outside the registry root.

    ``LocalLayoutRegistry.layout`` built ``self.root / reference`` and trusted the result.
    ``pathlib`` lets an absolute reference replace the root outright and a ``..`` reference walk out
    of it, so a reference arriving from configuration could read and write anywhere the process can.
    It is refused rather than sanitised: rewriting a reference would file an artifact under a name
    nobody asked for.
    """
    registry = LocalLayoutRegistry(tmp_path / "registry")
    with pytest.raises(DistributionError):
        registry.layout("../escaped/brain", create=True)


def test_malformed_layout_marker_raises_module_error(tmp_path: Path) -> None:
    """A corrupt ``oci-layout`` must be reported as a bad layout, not as a JSON error.

    ``_require_layout`` parsed the marker with an unguarded ``json.loads``, so the caller saw
    ``JSONDecodeError`` -- neither in the documented ``Raises`` nor catchable as the ``ModuleError``
    every other layout failure uses. A corrupt file on disk is a condition to report, not a bug.
    """
    (tmp_path / "blobs" / "sha256").mkdir(parents=True)
    (tmp_path / "oci-layout").write_text("not json")

    with pytest.raises(ModuleError):
        OciLayoutStore(tmp_path, create=False)


def test_opening_a_foreign_layout_is_refused(tmp_path: Path) -> None:
    """An existing directory declaring an unsupported layout version must be refused.

    The version check lived in ``_require_layout``, which only ran when ``create=False``.
    ``Brain.open`` always passes ``create=True``, so the check every caller relied on was unreachable
    from the public API and a foreign layout was adopted in silence -- then written into.
    """
    (tmp_path / "blobs" / "sha256").mkdir(parents=True)
    (tmp_path / "oci-layout").write_text('{"imageLayoutVersion": "9.9.9"}')

    with pytest.raises(ModuleError):
        OciLayoutStore(tmp_path)


# --- Correctness --------------------------------------------------------------------


def test_a_second_handle_sees_a_redaction_as_tombstoned(tmp_path: Path) -> None:
    """A handle opened before a redaction must still tell a tombstone from a missing block.

    ``OciLayoutStore._tombstones`` cached the file on first read for the life of the instance. A
    second handle on the same directory -- another process, or a long-lived reader beside a writer --
    kept the stale empty map, so ``has()`` answered False for a redacted block and it read as
    *missing*. Section 10.6 exists precisely to keep lawful erasure distinguishable from a corrupt
    store, and a reader alongside a writer is the ordinary deployment.
    """
    writer = Brain.open(tmp_path, actor=ACTOR, policy=REDACTING_POLICY)
    registration = writer.register(b"personal data", _request())
    reader = Brain.open(tmp_path, actor=ACTOR, policy=REDACTING_POLICY)
    assert reader.store.is_resolvable(registration.block_id)

    writer.redact(registration.block_id, MemoryType.CANONICAL, reason="erasure request")

    assert reader.store.has(registration.block_id)


def test_drop_by_producer_is_one_commit_and_reports_everything(tmp_path: Path) -> None:
    """Invalidating a producer's output must be one version, and must report all of it.

    ``drop_by_producer`` called ``drop`` once per memory type. Each call published its own snapshot,
    so one logical removal became N versions -- against ``_write``'s own "one commit is one version"
    -- and the returned ``DropResult`` was the last iteration's, leaving every earlier module absent
    from what the caller was told. A policy refusal part-way through left the earlier drops already
    committed, with nothing to roll them back.
    """
    brain = Brain.open(tmp_path, actor=ACTOR, policy=PERMISSIVE_POLICY)
    registration = brain.register(b"source", _request())
    task = brain.define_task(registration.block_id)
    producer = Producer(kind=ProducerKind.MODEL, id="retracted-model", version="1")

    brain.commit(
        brain.validate(
            CandidateSet(
                task_id=task.task_id,
                producer=producer,
                candidates=[
                    Candidate(
                        memory_type=MemoryType.SEMANTIC,
                        evidence=[registration.block_id],
                        payload={"label": "L", "statement": "S", "kind": "fact"},
                    ),
                    Candidate(
                        memory_type=MemoryType.PROCEDURAL,
                        evidence=[registration.block_id],
                        payload={"label": "P", "goal": "G", "steps": [{"action": "do it"}]},
                    ),
                ],
            ),
            task,
        )
    )

    before = len(brain.history())
    result = brain.drop_by_producer(
        ProducerDropRequest(
            producer=producer,
            memory_types=[MemoryType.SEMANTIC, MemoryType.PROCEDURAL],
            actor=ACTOR,
            reason="model retracted",
        )
    )

    assert len(brain.history()) - before == 1
    assert set(result.dropped) == {MemoryType.SEMANTIC, MemoryType.PROCEDURAL}


# --- Scaling ------------------------------------------------------------------------


def test_merkle_verify_scales_subquadratically() -> None:
    """Verifying a whole composition must not cost the square of its size.

    ``MerkleTree.verify`` builds a fresh proof per leaf, and ``_collect_path`` used to recompute each
    sibling subtree hash from the leaves. Doubling the leaves quadrupled the work: 2000 blocks took
    four seconds, and ``Brain.verify()`` -- the documented whole-module integrity check -- was
    unusable at the sizes the paper's distribution story assumes. Internal nodes are now derived once
    per tree, which is O(n) of them, so the same 2000 blocks take milliseconds.
    """

    def elapsed(count: int) -> float:
        leaves = [
            SemanticBlock(label=f"l{index}", statement=f"s{index}", kind=SemanticKind.FACT).block_id
            for index in range(count)
        ]
        tree = MerkleTree(leaves)
        start = time.perf_counter()
        tree.verify()
        return time.perf_counter() - start

    baseline = elapsed(500)
    doubled = elapsed(1000)
    assert doubled < baseline * 3


def test_planning_a_multi_block_drop_does_not_rescan_per_block() -> None:
    """A drop of k blocks must not cost k full passes over the module.

    ``plan_many`` calls ``plan_cascade`` per origin, and each call ran ``structural_dependents``,
    decoding every semantic and procedural block in the brain. Dropping k blocks from a module of n
    cost k*n decodes before anything was written, and the cascade's inner loop re-flattened the
    accumulated set on every iteration on top of that. The structural edges are now inverted once per
    batch, so planning is flat in the number of blocks named.
    """
    from boltzmann.module.composition import Composition
    from boltzmann.module.ledger import Ledger
    from boltzmann.module.module import Module
    from boltzmann.retention.cascade import plan_many

    store = MemoryBlockStore()
    blocks = [SemanticBlock(label=f"l{index}", statement=f"s{index}", kind=SemanticKind.FACT) for index in range(300)]
    for block in blocks:
        store.put_block(block)
    modules = {
        MemoryType.SEMANTIC: Module(
            MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [block.block_id for block in blocks])
        )
    }

    def elapsed(count: int) -> float:
        origins = [block.block_id for block in blocks[:count]]
        start = time.perf_counter()
        plan_many(origins, MemoryType.SEMANTIC, modules, Ledger())
        return time.perf_counter() - start

    baseline = elapsed(2)
    twentyfold = elapsed(40)
    assert twentyfold < baseline * 8


def test_the_validation_gate_types_each_candidate_once() -> None:
    """A candidate's payload must be parsed once and reused across the checks.

    ``SchemaValidator``, ``DuplicateValidator``, ``RelationValidator`` and ``ContradictionValidator``
    all need the candidate typed, and the gate needs it again for a validated result -- four parses
    of one payload, plus two independent walks of the whole semantic module once ``conflicts_for``
    ran the same scan ``ContradictionValidator`` had just finished.

    What is counted is ``_type_candidate``, the parse itself, rather than ``build_block``: the point
    is that the work happens once, not that the question is asked once.
    """
    from boltzmann.blocks.canonical import CanonicalBlock
    from boltzmann.identity.digest import OciDigest
    from boltzmann.ingest import validators
    from boltzmann.ingest.task import ProcessingTask, TaskOperation
    from boltzmann.ingest.validation import ValidationStatus, validate
    from boltzmann.module.composition import Composition
    from boltzmann.module.module import Module

    store = MemoryBlockStore()
    evidence = CanonicalBlock(blob=OciDigest.of(b"source"), media_type="text/plain", size=6)
    store.put_block(evidence)
    modules = {
        MemoryType.CANONICAL: Module(
            MemoryType.CANONICAL, store, Composition(MemoryType.CANONICAL, [evidence.block_id])
        ),
        MemoryType.SEMANTIC: Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC)),
    }

    parses = 0
    original = validators._type_candidate

    def counting(candidate: Candidate) -> object:
        nonlocal parses
        parses += 1
        return original(candidate)

    validators._type_candidate = counting  # type: ignore[assignment]
    try:
        task = ProcessingTask(
            operation=TaskOperation.EXTRACT_KNOWLEDGE,
            source=evidence.block_id,
            allowed_memory_types=[MemoryType.SEMANTIC],
        )
        candidates = CandidateSet(
            producer=Producer(kind=ProducerKind.MODEL, id="m"),
            candidates=[
                Candidate(
                    memory_type=MemoryType.SEMANTIC,
                    evidence=[evidence.block_id],
                    payload={"label": "x", "statement": "y", "kind": "fact"},
                )
            ],
        )
        report = validate(candidates, task, modules)
    finally:
        validators._type_candidate = original  # type: ignore[assignment]

    # The validated path is the expensive one: four checks need the block and the gate needs it again.
    assert report.results[0].status is ValidationStatus.VALIDATED
    assert parses == 1


def test_the_validation_gate_scans_the_module_once_per_candidate() -> None:
    """A contradicted proposal must not walk the semantic module twice.

    ``ContradictionValidator`` scanned every held block to build its issues, and then the gate called
    ``conflicts_for`` -- which ran the identical scan again to name the same blocks. Both now share
    one pass, so the cost of a contradiction is linear in the module rather than twice that.
    """
    from boltzmann.identity.digest import BlockId
    from boltzmann.ingest import validators
    from boltzmann.ingest.task import ProcessingTask, TaskOperation
    from boltzmann.ingest.validation import validate
    from boltzmann.module.composition import Composition
    from boltzmann.module.module import Module

    store = MemoryBlockStore()
    held = SemanticBlock(label="x", statement="the held statement", kind=SemanticKind.FACT)
    store.put_block(held)
    modules = {
        MemoryType.SEMANTIC: Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [held.block_id]))
    }

    scans = 0
    original = validators._scan_for_conflicts

    def counting(candidate: Candidate, passed: dict) -> object:
        nonlocal scans
        scans += 1
        return original(candidate, passed)

    validators._scan_for_conflicts = counting  # type: ignore[assignment]
    try:
        source = BlockId.of(b"source")
        task = ProcessingTask(
            operation=TaskOperation.EXTRACT_KNOWLEDGE, source=source, allowed_memory_types=[MemoryType.SEMANTIC]
        )
        candidates = CandidateSet(
            producer=Producer(kind=ProducerKind.MODEL, id="m"),
            candidates=[
                Candidate(
                    memory_type=MemoryType.SEMANTIC,
                    evidence=[source],
                    payload={"label": "x", "statement": "a different statement", "kind": "fact"},
                )
            ],
        )
        report = validate(candidates, task, modules)
    finally:
        validators._scan_for_conflicts = original  # type: ignore[assignment]

    assert report.results[0].conflicts_with == [held.block_id]
    assert scans == 1
