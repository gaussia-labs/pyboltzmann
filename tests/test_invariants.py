"""The eight protocol invariants, each as an executable claim.

The paper states these as normative rules. This file is the check that the SDK makes them
structural -- that violating one is an error rather than a matter of remembering not to.
"""

import pytest
from pydantic import ValidationError

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import RemovalMechanism, RemovalRecord
from boltzmann.blocks.semantic import SemanticBlock, SemanticKind
from boltzmann.exceptions import (
    AppendOnlyViolationError,
    DigestKindError,
    MembershipError,
    NonDeterministicValueError,
    RetentionPolicyError,
)
from boltzmann.identity.digest import BlockId, MerkleRoot, OciDigest
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.task import PROPOSABLE_MEMORY_TYPES, ProcessingTask, TaskOperation
from boltzmann.module.composition import Composition
from boltzmann.module.module import Module
from boltzmann.module.snapshot import ModuleRef
from boltzmann.protocol.operations import (
    BoltzmannProtocol,
    BrainDistribution,
    BrainReader,
    BrainRetention,
    BrainWriter,
)
from boltzmann.query.evidence import EvidenceBundle, Match
from boltzmann.query.request import Query, QueryFilters, QueryHints
from boltzmann.retention.policy import PERMISSIVE_POLICY, RetentionPolicy
from boltzmann.store.memory import MemoryBlockStore


def semantic(label: str = "Fourier series") -> SemanticBlock:
    return SemanticBlock(kind=SemanticKind.FORMULA, label=label, statement="f(x) = ...")


class TestInvariant1LlmNeverWrites:
    """The LLM never writes directly to the Merkle DAGs or to the indices (Section 7.1)."""

    def test_a_candidate_is_not_a_block(self) -> None:
        """A proposal has no identity, so it cannot be committed by mistake."""
        candidate = Candidate(
            memory_type=MemoryType.SEMANTIC,
            payload={"kind": "formula", "label": "l", "statement": "s"},
            evidence=[BlockId.of(b"pdf")],
        )
        assert not isinstance(candidate, Block)
        assert not hasattr(candidate, "block_id")

    def test_a_candidate_set_carries_no_roots(self) -> None:
        """Nothing a proposer returns can name or advance a composition."""
        fields = set(CandidateSet.model_fields) | set(Candidate.model_fields)
        assert not {field for field in fields if "root" in field or "merkle" in field}

    def test_a_module_exposes_no_write_method(self) -> None:
        """Deriving returns a new module; there is no in-place mutation to reach for."""
        writers = {name for name in dir(Module) if name in {"put", "write", "insert", "update", "save", "add"}}
        assert not writers

    def test_a_task_cannot_invite_writing_evidence_or_the_ledger(self) -> None:
        source = BlockId.of(b"pdf")
        for forbidden in (MemoryType.CANONICAL, MemoryType.PROVENANCE):
            with pytest.raises(ValidationError, match="cannot allow"):
                ProcessingTask(
                    operation=TaskOperation.EXTRACT_KNOWLEDGE,
                    source=source,
                    allowed_memory_types=[forbidden],
                )

    def test_only_interpretation_is_proposable(self) -> None:
        assert {MemoryType.EPISODIC, MemoryType.SEMANTIC, MemoryType.PROCEDURAL} == PROPOSABLE_MEMORY_TYPES


class TestInvariant2ThreeLevelsOfHashes:
    """Block id, Merkle root, and OCI digest are distinct kinds of identity (Section 6.4)."""

    def test_none_of_them_is_a_string(self) -> None:
        for digest in (BlockId.of(b"x"), MerkleRoot.of(b"x"), OciDigest.of(b"x")):
            assert not isinstance(digest, str)

    def test_they_do_not_compare_equal(self) -> None:
        assert BlockId.of(b"x") != MerkleRoot.of(b"x") != OciDigest.of(b"x")

    def test_a_root_cannot_pose_as_a_block_id(self) -> None:
        with pytest.raises(DigestKindError):
            BlockId.parse(MerkleRoot.of(b"x"))

    def test_pydantic_refuses_the_wrong_level(self) -> None:
        with pytest.raises(ValidationError):
            ModuleRef(
                memory_type=MemoryType.SEMANTIC,
                root=OciDigest.of(b"a file"),
                composition=OciDigest.of(b"leaves"),
                block_count=0,
            )

    def test_each_level_names_itself(self) -> None:
        assert (BlockId.KIND, MerkleRoot.KIND, OciDigest.KIND) == ("block_id", "merkle_root", "oci_digest")


class TestInvariant3BlocksAreImmutable:
    """Blocks never change; compositions do."""

    def test_a_block_cannot_be_edited(self) -> None:
        with pytest.raises(ValidationError):
            semantic().statement = "something else"  # type: ignore[misc]

    def test_correcting_a_block_creates_a_new_one(self) -> None:
        original = semantic()
        corrected = original.model_copy(update={"statement": "corrected"})
        assert corrected.block_id != original.block_id

    def test_dropping_changes_the_composition_not_the_block(self) -> None:
        block = semantic()
        composition = Composition(MemoryType.SEMANTIC, [block.block_id])
        reduced = composition.drop([block.block_id])
        assert block.block_id not in reduced
        assert block.block_id == semantic().block_id


class TestInvariant4QueriesNameNoIndex:
    """Queries are declarative and index-agnostic (Principle 7)."""

    def test_no_query_field_names_an_index(self) -> None:
        fields = set(Query.model_fields) | set(QueryFilters.model_fields) | set(QueryHints.model_fields)
        assert not {field for field in fields if "index" in field}

    def test_retrieval_mode_names_a_strategy_not_an_engine(self) -> None:
        from boltzmann.query.request import RetrievalMode

        assert {mode.value for mode in RetrievalMode} == {
            "auto",
            "exact",
            "lexical",
            "semantic",
            "associative",
        }


class TestInvariant5NeverProse:
    """The brain returns data, never a written answer (Section 9.3)."""

    def test_the_bundle_has_no_answer_field(self) -> None:
        assert set(EvidenceBundle.model_fields) == {"matches", "verified_against", "truncated"}

    def test_a_match_has_no_answer_field(self) -> None:
        forbidden = {"answer", "text", "response", "summary", "explanation", "prose"}
        assert not forbidden & set(Match.model_fields)

    def test_a_match_carries_content_and_provenance(self) -> None:
        assert {"block_id", "memory_type", "content", "sources", "score", "verified"} <= set(Match.model_fields)

    def test_scores_are_strings(self) -> None:
        """A number whose textual form varies across languages does not belong in a wire format."""
        assert Match.model_fields["score"].annotation is str


class TestInvariant6EverythingIsVerified:
    """Every returned block is checked by hash and by membership (Section 9.2)."""

    def test_reading_outside_the_composition_is_refused(self) -> None:
        store = MemoryBlockStore()
        kept, dropped = semantic("kept"), semantic("dropped")
        store.put_block(kept)
        store.put_block(dropped)
        module = Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [kept.block_id]))
        with pytest.raises(MembershipError):
            module.get(dropped.block_id)

    def test_an_unverified_match_is_rejected_by_the_consumer(self) -> None:
        bundle = EvidenceBundle(
            matches=[
                Match(
                    block_id=semantic().block_id,
                    memory_type=MemoryType.SEMANTIC,
                    content={},
                    score="1",
                    verified=False,
                )
            ]
        )
        assert not bundle.all_verified
        with pytest.raises(MembershipError, match="must never return"):
            bundle.require_verified()

    def test_a_bundle_records_what_it_was_verified_against(self) -> None:
        assert "verified_against" in EvidenceBundle.model_fields


class TestInvariant7EpisodicIsAppendOnly:
    """The chronological record is never rewritten (Section 10.3)."""

    def test_the_composition_refuses(self) -> None:
        target = BlockId.of(b"an episode")
        with pytest.raises(AppendOnlyViolationError):
            Composition(MemoryType.EPISODIC, [target]).drop([target])

    def test_no_policy_can_permit_it(self) -> None:
        """Append-only is a property of the protocol, not a setting."""
        permissive = RetentionPolicy(
            droppable_modules=list(MemoryType),
            canonical_drop_allowed=True,
            redactable_media_types=["*/*"],
        )
        with pytest.raises(RetentionPolicyError, match="no policy can permit"):
            permissive.authorize(RemovalMechanism.DROP, MemoryType.EPISODIC)

    def test_the_enum_says_so(self) -> None:
        for memory_type in MemoryType:
            assert memory_type.is_append_only is (memory_type is MemoryType.EPISODIC)


class TestInvariant8ForgettingIsAudited:
    """Every removal is explicit, recorded in provenance, and reportable (Principle 8)."""

    def test_recording_cannot_be_turned_off(self) -> None:
        assert RetentionPolicy().record_removals
        assert PERMISSIVE_POLICY.record_removals
        assert "record_removals" not in RetentionPolicy.model_fields

    def test_a_removal_record_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError):
            RemovalRecord(
                blocks=[BlockId.of(b"wrong")],
                mechanism=RemovalMechanism.DROP,
                memory_type=MemoryType.SEMANTIC,
                actor={"id": "alex", "kind": "human"},
                at="2026-07-24T09:30:00Z",
                reason="",
            )

    def test_canonical_drops_are_privileged_by_default(self) -> None:
        """Excluding evidence forfeits re-derivation, so it is off unless a policy says otherwise."""
        with pytest.raises(RetentionPolicyError, match="privileged"):
            RetentionPolicy().authorize(RemovalMechanism.DROP, MemoryType.CANONICAL)
        PERMISSIVE_POLICY.authorize(RemovalMechanism.DROP, MemoryType.CANONICAL)

    @pytest.mark.parametrize(
        "mechanism",
        [RemovalMechanism.TOMBSTONE, RemovalMechanism.CRYPTO_SHRED, RemovalMechanism.LINEAGE_REWRITE],
    )
    def test_redaction_is_refused_by_default(self, mechanism: RemovalMechanism) -> None:
        """Wrong knowledge is dropped, not redacted, so redaction needs explicit opt-in."""
        with pytest.raises(RetentionPolicyError, match="dropped, not redacted"):
            PERMISSIVE_POLICY.authorize(mechanism, MemoryType.SEMANTIC)

    def test_a_cascade_can_be_held_for_review(self) -> None:
        policy = RetentionPolicy(cascade_review_threshold=5)
        assert not policy.requires_review(5)
        assert policy.requires_review(6)
        assert not RetentionPolicy().requires_review(10_000)


class TestProtocolSurface:
    """The contract is declared, and the SDK implements none of it."""

    def test_the_sdk_ships_the_operations(self) -> None:
        """A client SDK has to work: you instantiate a class and the methods do the protocol's part."""
        from boltzmann import Brain

        for operation in ("register", "replace", "define_task", "validate", "commit", "ingest"):
            assert callable(getattr(Brain, operation))

    def test_the_sdk_ships_no_judgment(self) -> None:
        """What to ingest and how to rank are the implementer's, so nothing here decides them."""
        from boltzmann import indices, query

        assert not hasattr(query, "HybridPlanner")
        assert not hasattr(query, "reciprocal_rank_fusion")
        concrete = [
            name
            for name in dir(indices)
            if isinstance(getattr(indices, name), type)
            and issubclass(getattr(indices, name), indices.AbstractIndex)
            and getattr(indices, name) is not indices.AbstractIndex
        ]
        assert not concrete
        with pytest.raises(ModuleNotFoundError):
            __import__("boltzmann.adapters")

    def test_read_only_conformance_is_possible(self) -> None:
        """A client that only reads should not have to pretend to support writes it will refuse."""

        class ReadOnly:
            def snapshot(self): ...
            def root_of(self, memory_type): ...
            def module(self, memory_type): ...
            def open_index(self, memory_type, kind): ...
            def resolve(self, block_id): ...
            def prove(self, block_id, memory_type): ...
            def resolvability(self): ...
            def verify(self): ...
            def search(self, query): ...

        client = ReadOnly()
        assert isinstance(client, BrainReader)
        assert not isinstance(client, BrainWriter)
        assert not isinstance(client, BoltzmannProtocol)

    def test_the_full_protocol_composes_the_four_paths(self) -> None:
        assert issubclass(BoltzmannProtocol, BrainReader)
        assert issubclass(BoltzmannProtocol, BrainWriter)
        assert issubclass(BoltzmannProtocol, BrainRetention)
        assert issubclass(BoltzmannProtocol, BrainDistribution)

    def test_no_protocol_method_mentions_a_model(self) -> None:
        """The protocol embeds no LLM (Principle 5)."""
        for surface in (BrainReader, BrainWriter, BrainRetention, BrainDistribution):
            names = [name for name in dir(surface) if not name.startswith("_")]
            assert not [name for name in names if "model" in name.lower() or "llm" in name.lower()]

    def test_every_paper_operation_is_declared(self) -> None:
        """Section 7 enumerates the operations; none may be missing from the surface."""
        declared = {name for name in dir(BoltzmannProtocol) if not name.startswith("_")}
        assert {
            "snapshot",
            "root_of",
            "open_index",
            "resolve",
            "prove",
            "resolvability",
            "register",
            "replace",
            "define_task",
            "validate",
            "commit",
            "search",
            "drop",
            "drop_by_producer",
            "supersede",
            "demote",
            "prune",
            "redact",
            "pack",
            "push",
            "pull",
        } <= declared


class TestPayloadDomainIsClosed:
    """No value without a portable canonical form can reach a hash."""

    def test_floats_are_refused_wherever_they_appear(self) -> None:
        from boltzmann.identity.serialization import canonicalize

        for value in ({"x": 1.0}, {"x": [1.0]}, {"x": {"y": 1.0}}, [1.0]):
            with pytest.raises(NonDeterministicValueError):
                canonicalize(value)
