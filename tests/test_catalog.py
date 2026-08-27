"""Catalog structure is portable; hierarchy and paths are deterministic derived views."""

from __future__ import annotations

import pytest

from boltzmann import (
    Actor,
    ActorKind,
    Block,
    BlockId,
    Brain,
    BrainCatalog,
    CatalogError,
    ClassDeclaration,
    HierarchyDeclaration,
    MemoryBlockStore,
    MemoryType,
    PlacementDeclaration,
    Query,
    QueryFilters,
    RegistrationRequest,
    SchemeDeclaration,
    SemanticBlockV3,
    ValidationStatus,
)
from boltzmann.blocks.semantic import Relation, SemanticKind
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.module.ledger import Ledger
from boltzmann.reconcile.gate import judge_incoming
from boltzmann.retention.policy import PERMISSIVE_POLICY
from boltzmann.retention.requests import DropRequest

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)
REQUEST = RegistrationRequest(media_type="text/plain", actor=CURATOR)


@pytest.fixture
def brain() -> Brain:
    return Brain(MemoryBlockStore(), actor=CURATOR, policy=PERMISSIVE_POLICY)


CatalogMember = SchemeDeclaration | ClassDeclaration


def register(brain: Brain, text: str) -> BlockId:
    return brain.register(text.encode(), REQUEST).block_id


def taxonomy(brain: Brain) -> dict[str, CatalogMember]:
    year = SchemeDeclaration(scheme="year", exclusive=True)
    topic = SchemeDeclaration(scheme="topic")
    kind = SchemeDeclaration(scheme="type", exclusive=True)
    year_2025 = ClassDeclaration(scheme="year", label="2025")
    math = ClassDeclaration(scheme="topic", label="math")
    fourier = ClassDeclaration(scheme="topic", label="fourier")
    exams = ClassDeclaration(scheme="type", label="examenes")
    result = brain.classify(
        [
            year,
            topic,
            kind,
            year_2025,
            math,
            fourier,
            exams,
            HierarchyDeclaration(broader=math.block_id, narrower=fourier.block_id),
        ]
    )
    assert result.is_clean
    return {
        "year": year,
        "topic": topic,
        "type": kind,
        "2025": year_2025,
        "math": math,
        "fourier": fourier,
        "examenes": exams,
    }


class TestPortableRepresentation:
    def test_catalog_blocks_round_trip_as_semantic_v3(self) -> None:
        scheme = SchemeDeclaration(scheme="year", exclusive=True)
        parent = ClassDeclaration(scheme="year", label="2025")
        hierarchy = HierarchyDeclaration(
            broader=parent.block_id,
            narrower=ClassDeclaration(scheme="year", label="2024").block_id,
        )
        for declaration in (scheme, parent, hierarchy):
            block = declaration.to_block()
            decoded = Block.decode(block.canonical_bytes())
            assert isinstance(decoded, SemanticBlockV3)
            assert decoded == block

    def test_a_class_identity_does_not_contain_its_parent(self) -> None:
        child = ClassDeclaration(scheme="topic", label="fourier")
        first = HierarchyDeclaration(
            broader=ClassDeclaration(scheme="topic", label="math").block_id, narrower=child.block_id
        )
        second = HierarchyDeclaration(
            broader=ClassDeclaration(scheme="topic", label="signal-processing").block_id,
            narrower=child.block_id,
        )
        assert first.block_id != second.block_id
        assert child.to_block().payload() == {"kind": "class", "label": "fourier", "scheme": "topic"}

    def test_catalog_structure_has_no_derivation_record(self, brain: Brain) -> None:
        result = brain.classify([SchemeDeclaration(scheme="topic"), ClassDeclaration(scheme="topic", label="math")])
        assert len(result.commit.committed) == 2
        assert result.commit.provenance == []


class TestValidation:
    def test_one_batch_can_reference_earlier_declarations(self, brain: Brain) -> None:
        scheme = SchemeDeclaration(scheme="topic")
        parent = ClassDeclaration(scheme="topic", label="math")
        child = ClassDeclaration(scheme="topic", label="fourier")
        result = brain.classify(
            [scheme, parent, child, HierarchyDeclaration(broader=parent.block_id, narrower=child.block_id)]
        )
        assert result.is_clean
        assert len(result.commit.committed) == 4

    def test_classes_require_a_declared_scheme(self, brain: Brain) -> None:
        result = brain.classify([ClassDeclaration(scheme="missing", label="orphan")])
        assert result.verdicts[0].status is ValidationStatus.REJECTED
        assert result.verdicts[0].issues[0].code == "catalog-unknown-scheme"
        assert result.commit.is_empty

    def test_hierarchy_stays_in_one_scheme_and_is_acyclic(self, brain: Brain) -> None:
        a = SchemeDeclaration(scheme="a")
        b = SchemeDeclaration(scheme="b")
        one = ClassDeclaration(scheme="a", label="one")
        two = ClassDeclaration(scheme="a", label="two")
        other = ClassDeclaration(scheme="b", label="other")
        brain.classify([a, b, one, two, other, HierarchyDeclaration(broader=one.block_id, narrower=two.block_id)])

        cross = brain.classify([HierarchyDeclaration(broader=one.block_id, narrower=other.block_id)])
        cycle = brain.classify([HierarchyDeclaration(broader=two.block_id, narrower=one.block_id)])

        assert cross.verdicts[0].issues[0].code == "catalog-cross-scheme"
        assert cycle.verdicts[0].issues[0].code == "catalog-cycle"

    def test_polyhierarchy_is_allowed(self, brain: Brain) -> None:
        scheme = SchemeDeclaration(scheme="topic")
        left = ClassDeclaration(scheme="topic", label="analysis")
        right = ClassDeclaration(scheme="topic", label="signals")
        child = ClassDeclaration(scheme="topic", label="fourier")
        result = brain.classify(
            [
                scheme,
                left,
                right,
                child,
                HierarchyDeclaration(broader=left.block_id, narrower=child.block_id),
                HierarchyDeclaration(broader=right.block_id, narrower=child.block_id),
            ]
        )
        assert result.is_clean
        assert set(brain.browse(child.block_id).nodes[0].broader) == {left.block_id, right.block_id}

    def test_an_exclusive_scheme_reports_a_contradiction(self, brain: Brain) -> None:
        source = register(brain, "exam")
        scheme = SchemeDeclaration(scheme="year", exclusive=True)
        first = ClassDeclaration(scheme="year", label="2025")
        second = ClassDeclaration(scheme="year", label="2026")
        brain.classify([scheme, first, second, PlacementDeclaration(source=source, class_id=first.block_id)])
        result = brain.classify([PlacementDeclaration(source=source, class_id=second.block_id)])
        assert result.verdicts[0].status is ValidationStatus.CONTRADICTED
        assert result.verdicts[0].issues[0].code == "catalog-exclusive-conflict"
        assert result.verdicts[0].conflicts_with

    def test_a_placement_requires_canonical_evidence_and_a_class(self, brain: Brain) -> None:
        taxonomy(brain)
        missing = ClassDeclaration(scheme="topic", label="not-installed")
        result = brain.classify(
            [
                PlacementDeclaration(
                    source=ClassDeclaration(scheme="x", label="not-a-source").block_id, class_id=missing.block_id
                )
            ]
        )
        assert result.verdicts[0].issues[0].code == "catalog-unknown-source"


class TestBrowseAndPaths:
    def test_a_parent_includes_descendant_placements(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        source = register(brain, "Fourier exam")
        brain.classify([PlacementDeclaration(source=source, class_id=classes["fourier"].block_id)])
        assert brain.browse(classes["math"].block_id).sources == [source]

    def test_a_path_is_the_intersection_of_facets(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        exam = register(brain, "Fourier exam")
        note = register(brain, "Fourier notes")
        brain.classify(
            [
                PlacementDeclaration(source=exam, class_id=classes["2025"].block_id),
                PlacementDeclaration(source=exam, class_id=classes["fourier"].block_id),
                PlacementDeclaration(source=exam, class_id=classes["examenes"].block_id),
                PlacementDeclaration(source=note, class_id=classes["2025"].block_id),
                PlacementDeclaration(source=note, class_id=classes["fourier"].block_id),
            ]
        )
        view = brain.catalog_path(("year", "topic", "type"))
        assert set(view.browse("/2025/fourier/").sources) == {exam, note}
        assert view.browse("2025/fourier/examenes").sources == [exam]
        assert view.iterdir("2025/fourier/examenes").sources == [exam]

    def test_directories_only_show_values_with_matching_sources(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        unused = ClassDeclaration(scheme="year", label="2026")
        brain.classify([unused])
        source = register(brain, "Fourier exam")
        brain.classify(
            [
                PlacementDeclaration(source=source, class_id=classes["2025"].block_id),
                PlacementDeclaration(source=source, class_id=classes["fourier"].block_id),
                PlacementDeclaration(source=source, class_id=classes["examenes"].block_id),
            ]
        )
        view = brain.catalog_path(("year", "topic", "type"))
        assert view.iterdir().directories == ["2025"]
        assert view.iterdir("2025").directories == ["fourier", "math"]
        assert view.iterdir("2025/fourier").directories == ["examenes"]

    def test_the_same_placements_support_an_alternate_view(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        source = register(brain, "Fourier exam")
        brain.classify(
            [
                PlacementDeclaration(source=source, class_id=classes["2025"].block_id),
                PlacementDeclaration(source=source, class_id=classes["fourier"].block_id),
                PlacementDeclaration(source=source, class_id=classes["examenes"].block_id),
            ]
        )
        assert brain.catalog_path(("type", "topic", "year")).browse("examenes/fourier/2025").sources == [source]

    def test_paths_are_exact_percent_decoded_and_reject_ambiguous_segments(self, brain: Brain) -> None:
        scheme = SchemeDeclaration(scheme="topic")
        class_ = ClassDeclaration(scheme="topic", label="signal processing")
        brain.classify([scheme, class_])
        source = register(brain, "notes")
        view = brain.catalog_path(("topic",))
        view.classify(source, "signal%20processing")
        assert view.browse("signal%20processing").sources == [source]
        with pytest.raises(CatalogError):
            view.browse("Signal%20Processing")
        for invalid in (".", "..", "one//two", "%2E%2E", "%2F", "%FF"):
            with pytest.raises(CatalogError):
                view.browse(invalid)

    def test_classification_requires_a_complete_path(self, brain: Brain) -> None:
        taxonomy(brain)
        source = register(brain, "exam")
        view = brain.catalog_path(("year", "topic", "type"))
        with pytest.raises(CatalogError, match="requires all 3"):
            view.classify(source, "2025/fourier")


class TestQueryAndLifecycle:
    def test_query_class_filters_are_and_and_descendant_inclusive(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        exam = register(brain, "exam")
        note = register(brain, "note")
        brain.classify(
            [
                PlacementDeclaration(source=exam, class_id=classes["2025"].block_id),
                PlacementDeclaration(source=exam, class_id=classes["fourier"].block_id),
                PlacementDeclaration(source=note, class_id=classes["fourier"].block_id),
            ]
        )
        query = Query(
            filters=QueryFilters(
                memory_types=[MemoryType.CANONICAL],
                classes=[classes["2025"].block_id, classes["math"].block_id],
            )
        )
        assert [match.block_id for match in brain.search(query).matches] == [exam]

    def test_one_evidence_source_must_satisfy_every_requested_class(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        first = register(brain, "year evidence")
        second = register(brain, "topic evidence")
        brain.classify(
            [
                PlacementDeclaration(source=first, class_id=classes["2025"].block_id),
                PlacementDeclaration(source=second, class_id=classes["fourier"].block_id),
            ]
        )
        candidate = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[first, second],
            payload={"kind": "fact", "label": "joint", "statement": "joint evidence"},
        )
        brain.commit(brain.validate(CandidateSet(candidates=[candidate]), brain.define_task(first)))
        query = Query(
            text="joint",
            filters=QueryFilters(classes=[classes["2025"].block_id, classes["fourier"].block_id]),
        )
        assert brain.search(query).matches == []

        brain.classify([PlacementDeclaration(source=first, class_id=classes["fourier"].block_id)])
        assert [match.content["label"] for match in brain.search(query).matches] == ["joint"]

    def test_catalog_rebuilds_when_a_brain_is_reopened(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        source = register(brain, "exam")
        brain.classify([PlacementDeclaration(source=source, class_id=classes["fourier"].block_id)])
        reopened = Brain(brain.store, actor=CURATOR)
        assert reopened.browse(classes["math"].block_id).sources == [source]

    def test_dropping_evidence_cascades_only_its_placements(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        source = register(brain, "exam")
        placement = PlacementDeclaration(source=source, class_id=classes["fourier"].block_id)
        brain.classify([placement])
        brain.drop(DropRequest(blocks=[source], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="expired"))
        semantic = brain.module(MemoryType.SEMANTIC)
        assert placement.block_id not in semantic
        assert classes["fourier"].block_id in semantic

    def test_a_model_may_propose_a_placement_but_not_taxonomy(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        source = register(brain, "exam")
        task = brain.define_task(source)
        placement = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[source],
            payload={
                "kind": "relation",
                "relations": [{"predicate": "classified_as", "target": str(classes["fourier"].block_id)}],
            },
        )
        report = brain.validate(CandidateSet(candidates=[placement]), task)
        assert report.is_clean

        scheme = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[source],
            payload={"kind": "scheme", "scheme": "model-owned", "exclusive": False},
        )
        rejected = brain.validate(CandidateSet(candidates=[scheme]), task)
        assert rejected.results[0].status is ValidationStatus.REJECTED

    def test_catalog_taxonomy_and_placements_survive_the_reconciliation_gate(self, brain: Brain) -> None:
        classes = taxonomy(brain)
        source = register(brain, "exam")
        brain.classify([PlacementDeclaration(source=source, class_id=classes["fourier"].block_id)])
        modules = brain.modules()
        semantic = modules[MemoryType.SEMANTIC]

        report = judge_incoming(
            incoming={MemoryType.SEMANTIC: semantic.block_ids},
            reconciled={memory_type: module.composition for memory_type, module in modules.items()},
            store=brain.store,
            ledger=Ledger.of(modules),
        )

        assert report.is_clean, [issue.detail for verdict in report.verdicts for issue in verdict.issues]

    def test_brain_implements_the_catalog_extension(self, brain: Brain) -> None:
        assert isinstance(brain, BrainCatalog)


def test_v3_rejects_malformed_catalog_relation_shapes() -> None:
    target = ClassDeclaration(scheme="topic", label="fourier").block_id
    with pytest.raises(ValueError, match="exactly one"):
        SemanticBlockV3(
            kind=SemanticKind.RELATION,
            relations=[Relation(predicate="classified_as", target=target)],
        )
