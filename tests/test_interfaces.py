"""The interfaces an implementation satisfies, and the fact that the SDK ships none of them.

This replaces what used to be an inventory of `NotImplementedError` stubs. There are no stubs now:
an operation is either implemented because every client must agree on it, or it is an interface for
someone else to implement. This file checks that boundary holds.
"""

import inspect

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.identity.digest import BlockId
from boltzmann.indices.base import AbstractIndex, Index, IndexKind
from boltzmann.ingest.pipelines import (
    NormalizationPipeline,
    available_pipelines,
    get_pipeline,
    register_pipeline,
)
from boltzmann.ingest.proposer import CandidateProposer, CandidateSet
from boltzmann.ingest.validation import ValidationIssue, Validator
from boltzmann.merkle.layout import MerkleLayout
from boltzmann.merkle.tree import DEFAULT_LAYOUT
from boltzmann.protocol.operations import BrainReader
from boltzmann.query.planner import QueryPlanner
from boltzmann.store.base import BlockStore
from boltzmann.store.memory import MemoryBlockStore

# Every interface an implementer may satisfy, and whether the SDK ships an implementation of it.
INTERFACES = [
    (BlockStore, True),
    (MerkleLayout, True),
    (Index, False),
    (QueryPlanner, False),
    (Validator, False),
    (CandidateProposer, False),
    (NormalizationPipeline, False),
    (BrainReader, False),
]

ALL_INTERFACES = [interface for interface, _ in INTERFACES]
"""Just the interfaces, for tests that do not care whether the SDK ships an implementation."""


class TestNoStubsRemain:
    """An unimplemented function is worse than an interface: it looks callable and is not."""

    def test_no_module_raises_not_implemented(self) -> None:
        import importlib
        import pkgutil

        import boltzmann

        offenders = []
        for found in pkgutil.walk_packages(boltzmann.__path__, "boltzmann."):
            module = importlib.import_module(found.name)
            source = inspect.getsource(module)
            if "NotImplementedError" in source:
                offenders.append(found.name)
        assert not offenders, f"these modules still stub an operation: {offenders}"

    def test_no_index_engine_ships(self) -> None:
        """Which engine backs an index is the implementation's choice."""
        from boltzmann import indices

        concrete = [
            name
            for name in dir(indices)
            if isinstance(getattr(indices, name), type)
            and issubclass(getattr(indices, name), AbstractIndex)
            and getattr(indices, name) is not AbstractIndex
        ]
        assert not concrete

    def test_no_adapter_ships(self) -> None:
        """CLI, MCP, and skill are exposure layers, not protocol."""
        with pytest.raises(ModuleNotFoundError):
            __import__("boltzmann.adapters")


class TestInterfacesAreRuntimeCheckable:
    """An implementer should be able to assert conformance, not just hope for it."""

    @pytest.mark.parametrize("interface", ALL_INTERFACES, ids=lambda value: value.__name__)
    def test_is_runtime_checkable(self, interface: type) -> None:
        """``isinstance`` must work, so an implementer can assert conformance instead of hoping."""
        assert getattr(interface, "_is_runtime_protocol", False)

    @pytest.mark.parametrize("interface", ALL_INTERFACES, ids=lambda value: value.__name__)
    def test_declares_at_least_one_member(self, interface: type) -> None:
        """Compared against ``object`` so a callable protocol counts by its ``__call__``."""
        assert set(dir(interface)) - set(dir(object))


class TestShippedImplementations:
    """The SDK implements exactly what every conforming client must compute identically."""

    def test_the_store_interface_has_a_reference(self) -> None:
        """Needed to run the conformance suite and to exercise the kernel at all."""
        assert isinstance(MemoryBlockStore(), BlockStore)

    def test_the_merkle_layout_has_a_reference(self) -> None:
        """Roots must be identical across clients, so the default layout cannot be left open."""
        assert isinstance(DEFAULT_LAYOUT, MerkleLayout)
        assert DEFAULT_LAYOUT.name == "rfc6962-sorted/1"

    def test_index_kinds_are_named_without_being_implemented(self) -> None:
        """The protocol names the query shapes; it does not ship the engines."""
        assert {kind.value for kind in IndexKind} == {
            "hash_map",
            "btree",
            "inverted",
            "vector",
            "graph",
            "bitmap",
        }


class TestSatisfyingAnInterface:
    """What it takes for an implementer's class to conform."""

    def test_a_minimal_index_conforms(self) -> None:
        class CountingIndex(AbstractIndex):
            KIND = IndexKind.HASH_MAP

            def __init__(self) -> None:
                self.members: set[BlockId] = set()

            def build(self, blocks):
                self.members = {block.block_id for block in blocks}

            def search(self, query, limit=10):
                block_id = BlockId.parse(query)
                return [(block_id, 1.0)] if block_id in self.members else []

        index = CountingIndex()
        assert isinstance(index, Index)
        assert index.rebuildable
        assert index.model_tag is None

    def test_a_minimal_validator_conforms(self) -> None:
        class RequiresEvidence:
            code = "requires-evidence"

            def check(self, candidate, task, modules):
                if candidate.evidence:
                    return []
                return [ValidationIssue(code=self.code, detail="no evidence cited")]

        assert isinstance(RequiresEvidence(), Validator)

    def test_a_minimal_proposer_conforms(self) -> None:
        """The SDK ships no proposer; anything with this shape is one."""

        class Refuses:
            def __call__(self, task, source):
                return CandidateSet()

        assert isinstance(Refuses(), CandidateProposer)

    def test_a_minimal_planner_conforms(self) -> None:
        class Empty:
            def plan(self, query, modules):
                from boltzmann.query.evidence import EvidenceBundle

                return EvidenceBundle()

        assert isinstance(Empty(), QueryPlanner)


class TestPipelineRegistry:
    """A normalized view is only evidence if the transform that made it is reproducible."""

    def make_pipeline(self, name: str = "text-extract") -> NormalizationPipeline:
        class Upper:
            @property
            def name(self) -> str:
                return name

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def output_media_type(self) -> str:
                return "text/plain"

            def accepts(self, media_type: str) -> bool:
                return media_type == "text/plain"

            def normalize(self, data: bytes) -> bytes:
                return data.upper()

        return Upper()

    def test_a_pipeline_conforms(self) -> None:
        assert isinstance(self.make_pipeline(), NormalizationPipeline)

    def test_registers_and_resolves_by_name(self) -> None:
        pipeline = self.make_pipeline("register-and-resolve")
        register_pipeline(pipeline)
        assert get_pipeline("register-and-resolve") is pipeline
        assert "register-and-resolve" in available_pipelines()

    def test_registering_the_same_pipeline_twice_is_idempotent(self) -> None:
        pipeline = self.make_pipeline("idempotent")
        register_pipeline(pipeline)
        register_pipeline(pipeline)
        assert get_pipeline("idempotent") is pipeline

    def test_a_name_collision_is_refused(self) -> None:
        """A name and version must identify exactly one transform, or the view is not reproducible."""
        register_pipeline(self.make_pipeline("collision"))
        with pytest.raises(Exception, match="already registered"):
            register_pipeline(self.make_pipeline("collision"))

    def test_an_unknown_name_is_refused(self) -> None:
        with pytest.raises(Exception, match="no normalization pipeline registered"):
            get_pipeline("does-not-exist")

    def test_the_transform_is_deterministic(self) -> None:
        pipeline = self.make_pipeline("deterministic")
        assert pipeline.normalize(b"fourier") == pipeline.normalize(b"fourier")


class TestMemoryTypeCoverage:
    """The five modules, and which of them the protocol treats specially."""

    def test_all_five_exist(self) -> None:
        assert {kind.value for kind in MemoryType} == {
            "canonical",
            "episodic",
            "semantic",
            "procedural",
            "provenance",
        }

    def test_exactly_one_is_append_only(self) -> None:
        assert [kind for kind in MemoryType if kind.is_append_only] == [MemoryType.EPISODIC]

    def test_derived_modules_are_the_cascade_targets(self) -> None:
        assert {kind for kind in MemoryType if kind.is_derived} == {
            MemoryType.SEMANTIC,
            MemoryType.PROCEDURAL,
        }
