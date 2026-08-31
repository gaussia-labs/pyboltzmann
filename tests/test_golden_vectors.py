"""The golden vectors must keep describing this implementation.

These files ship in the wheel so an implementation in another language can check itself against
the same cases. Running them here is what makes them trustworthy: if a change to the kernel would
alter an identity, this test fails before the vectors go stale.

A failure here means one of two things. Either the change is a bug, or it is a deliberate change to
the serialization -- in which case it needs a new serialization identifier, not a regenerated file.
"""

import json
import subprocess
import sys
from textwrap import dedent

import pytest
from pydantic import ValidationError as PydanticValidationError

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.semantic import SemanticBlock
from boltzmann.conformance import golden
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.exceptions import SerializationError
from boltzmann.identity.digest import BlockId
from boltzmann.identity.serialization import SERIALIZATION_ID, canonicalize, parse_json_strict
from boltzmann.merkle.proof import InclusionProof
from boltzmann.merkle.tree import LAYOUT_NAME, merkle_root


class TestVectorFiles:
    """Every published file must load and declare what produced it."""

    @pytest.mark.parametrize("name", golden.VECTOR_FILES)
    def test_declares_its_provenance(self, name: str) -> None:
        vectors = golden.load(name)
        assert vectors["boltzmann"] == PROTOCOL_VERSION
        assert vectors["serialization"] == SERIALIZATION_ID
        assert vectors["hash"] == "sha256"
        # The identity files carry `vectors`; the judgement file carries `cases`. Either way, an
        # empty published file is a promise with nothing behind it.
        assert vectors.get("vectors") or vectors.get("cases")

    def test_all_files_load(self) -> None:
        assert set(golden.load_all()) == set(golden.VECTOR_FILES)


class TestBlockIdVectors:
    """Identity must still be what the published vectors say it is."""

    @pytest.mark.parametrize("vector", golden.load("block_ids.json")["vectors"], ids=lambda v: v["name"])
    def test_bytes_hash_to_the_published_block_id(self, vector: dict) -> None:
        assert str(BlockId.of(vector["canonical_bytes"].encode())) == vector["block_id"]

    @pytest.mark.parametrize("vector", golden.load("block_ids.json")["vectors"], ids=lambda v: v["name"])
    def test_envelope_canonicalizes_to_the_published_bytes(self, vector: dict) -> None:
        assert canonicalize(vector["envelope"]).decode() == vector["canonical_bytes"]

    @pytest.mark.parametrize("vector", golden.load("block_ids.json")["vectors"], ids=lambda v: v["name"])
    def test_the_bytes_decode_and_reproduce_their_identity(self, vector: dict) -> None:
        block = Block.decode(vector["canonical_bytes"].encode())
        assert block.MEMORY_TYPE.value == vector["memory_type"]
        assert vector["schema_version"] == block.SCHEMA_VERSION
        assert str(block.block_id) == vector["block_id"]

    def test_every_memory_type_is_covered(self) -> None:
        covered = {vector["memory_type"] for vector in golden.load("block_ids.json")["vectors"]}
        assert covered == {"canonical", "episodic", "semantic", "procedural", "provenance"}

    def test_every_registered_schema_version_is_covered(self) -> None:
        """A memory type is not a schema. Once one has two versions, only one of them was pinned.

        These vectors are what another implementation compares itself against, so a version with no
        vector is a version two clients can disagree about while both pass their own suites. Adding
        a schema therefore means adding a vector -- appending one is allowed, changing one is not.
        """
        covered = {
            (vector["memory_type"], vector["schema_version"]) for vector in golden.load("block_ids.json")["vectors"]
        }
        registered = {(memory_type.value, version) for memory_type, version in Block.registry()}
        assert registered - covered == set(), "registered schemas with no golden vector"


class TestMerkleRootVectors:
    """Roots must still be what the published vectors say they are."""

    @pytest.mark.parametrize("vector", golden.load("merkle_roots.json")["vectors"], ids=lambda v: v["name"])
    def test_composition_produces_the_published_root(self, vector: dict) -> None:
        assert vector["layout"] == LAYOUT_NAME
        leaves = [BlockId.parse(value) for value in vector["block_ids"]]
        assert str(merkle_root(leaves)) == vector["root"]

    def test_the_empty_composition_is_covered(self) -> None:
        sizes = {len(vector["block_ids"]) for vector in golden.load("merkle_roots.json")["vectors"]}
        assert 0 in sizes

    def test_sizes_around_powers_of_two_are_covered(self) -> None:
        """The split point of RFC 9162 is where an off-by-one would hide."""
        sizes = {len(vector["block_ids"]) for vector in golden.load("merkle_roots.json")["vectors"]}
        assert {1, 2, 3, 4, 5, 7, 8, 9, 16, 17} <= sizes


class TestInclusionProofVectors:
    """Published proofs must still verify."""

    @pytest.mark.parametrize("vector", golden.load("inclusion_proofs.json")["vectors"], ids=lambda v: v["name"])
    def test_proof_verifies_against_its_root(self, vector: dict) -> None:
        from boltzmann.identity.digest import MerkleRoot

        proof = InclusionProof(
            block_id=BlockId.parse(vector["block_id"]),
            leaf_index=vector["leaf_index"],
            tree_size=vector["tree_size"],
            audit_path=vector["audit_path"],
        )
        assert proof.verify(MerkleRoot.parse(vector["root"]))

    def test_every_leaf_of_the_odd_sized_tree_is_covered(self) -> None:
        """An odd tree is where a wrong split is easiest to miss, so every position is proved."""
        vectors = [v for v in golden.load("inclusion_proofs.json")["vectors"] if v["tree_size"] == 11]
        assert {vector["leaf_index"] for vector in vectors} == set(range(11))

    def test_the_sizes_the_paper_names_are_all_present(self) -> None:
        """Size one, a power of two, and an odd size: the three shapes the split rule behaves
        differently on, and the three the golden-vector bullet names."""
        sizes = {vector["tree_size"] for vector in golden.load("inclusion_proofs.json")["vectors"]}
        assert 1 in sizes
        assert any(size > 1 and size & (size - 1) == 0 for size in sizes)
        assert any(size % 2 for size in sizes if size > 1)


ACCEPTED = [v for v in golden.load("serialization.json")["vectors"] if not v.get("rejected")]
REJECTED = [v for v in golden.load("serialization.json")["vectors"] if v.get("rejected")]


class TestSerializationVectors:
    """Canonicalization must still produce the published bytes."""

    @pytest.mark.parametrize("vector", ACCEPTED, ids=lambda v: v["name"])
    def test_value_canonicalizes_to_the_published_bytes(self, vector: dict) -> None:
        assert canonicalize(vector["value"]).decode() == vector["canonical_bytes"]

    @pytest.mark.parametrize("vector", ACCEPTED, ids=lambda v: v["name"])
    def test_bytes_hash_to_the_published_digest(self, vector: dict) -> None:
        assert str(BlockId.of(vector["canonical_bytes"].encode())) == vector["sha256"]

    @pytest.mark.parametrize("vector", REJECTED, ids=lambda v: v["name"])
    def test_a_document_the_corpus_rejects_is_rejected_here(self, vector: dict) -> None:
        """The rejections carry as much weight as the acceptances: they are the cases where a
        permissive parser silently reads a document as something another parser would not."""
        with pytest.raises(SerializationError):
            parse_json_strict(vector["document"].encode())

    def test_the_corpus_carries_cases_of_both_kinds(self) -> None:
        assert ACCEPTED
        assert REJECTED

    def test_the_non_bmp_key_sorts_by_code_unit_not_code_point(self) -> None:
        """The one serialization case where an implementation can be self-consistent and still wrong.

        Above U+FFFF a key is encoded as a surrogate pair, which sorts below U+FF5E by UTF-16 code
        unit and above it by code point. Sorting by code point produces different bytes -- and so a
        different block_id -- for identical knowledge.
        """
        vector = next(v for v in ACCEPTED if v["name"] == "non_bmp_key_ordering")
        keys = list(json.loads(vector["canonical_bytes"]))
        assert keys == sorted(keys, key=lambda key: key.encode("utf-16-be"))
        assert keys != sorted(keys)

    def test_the_safe_integer_boundary_is_covered(self) -> None:
        names = {vector["name"] for vector in golden.load("serialization.json")["vectors"]}
        assert "safe_integer_bounds" in names


class TestSchemaSelectionVectors:
    """Oldest-that-fits, checked against the registered set rather than against this SDK's opinion."""

    @pytest.mark.parametrize(
        "vector",
        [v for v in golden.load("schema_selection.json")["vectors"] if not v.get("refused")],
        ids=lambda v: v["name"],
    )
    def test_the_payload_is_written_under_the_published_version(self, vector: dict) -> None:
        block = Block.build(MemoryType(vector["memory_type"]), vector["payload"])
        assert vector["schema_version"] == block.SCHEMA_VERSION
        assert str(block.block_id) == vector["block_id"]

    @pytest.mark.parametrize(
        "vector",
        [v for v in golden.load("schema_selection.json")["vectors"] if not v.get("refused")],
        ids=lambda v: v["name"],
    )
    def test_the_published_version_is_the_oldest_that_fits(self, vector: dict) -> None:
        assert vector["schema_version"] == min(vector["satisfies"])

    @pytest.mark.parametrize(
        "vector",
        [v for v in golden.load("schema_selection.json")["vectors"] if v.get("refused")],
        ids=lambda v: v["name"],
    )
    def test_a_payload_satisfying_no_registered_schema_is_refused(self, vector: dict) -> None:
        """Not every payload has an answer, and pretending otherwise is how one gets invented.

        Evolution is usually additive, so oldest-that-fits is usually a choice among schemas that
        all accept the payload. A version that *removes* a required member makes the two disjoint
        instead, and then a payload can satisfy neither -- carrying both members, or neither. The
        rule needs no amendment for it, because "satisfies" already admits no member a schema does
        not name; what it needs is for an implementation to refuse rather than pick the closest.
        """
        assert vector["satisfies"] == []
        assert "schema_version" not in vector
        with pytest.raises(PydanticValidationError):
            Block.build(MemoryType(vector["memory_type"]), vector["payload"])

    def test_a_payload_satisfying_several_schemas_is_covered(self) -> None:
        """The case the rule exists for. Without it the vectors would pin only the easy answers."""
        vectors = golden.load("schema_selection.json")["vectors"]
        assert any(len(vector["satisfies"]) > 1 for vector in vectors)

    def test_both_sides_of_a_disjoint_pair_are_covered(self) -> None:
        """A removal is the case oldest-that-fits had never met, so it gets both halves and both
        refusals rather than one example and an assurance."""
        vectors = golden.load("schema_selection.json")["vectors"]
        assert sum(1 for vector in vectors if vector.get("refused")) >= 2


class TestReconciliationVectors:
    """Equation 4 as set arithmetic, including the case where exclusion has to win."""

    @pytest.mark.parametrize("vector", golden.load("reconciliation.json")["vectors"], ids=lambda v: v["name"])
    def test_the_merge_produces_the_published_set(self, vector: dict) -> None:
        base = {BlockId.parse(value) for value in vector["ancestor"]}
        ours = {BlockId.parse(value) for value in vector["ours"]}
        theirs = {BlockId.parse(value) for value in vector["theirs"]}

        merged = (base | ours | theirs) - ((base - ours) | (base - theirs))

        assert sorted(str(block) for block in merged) == vector["merged"]
        assert str(merkle_root(merged)) == vector["merged_root"]

    def test_the_refusals_the_paper_names_are_published(self) -> None:
        """A corpus of results only would let an implementation pass while merging a criss-cross."""
        conditions = {refusal["condition"] for refusal in golden.load("reconciliation.json")["refusals"]}
        assert conditions == {"no_common_ancestor", "multiple_merge_bases"}


class TestTheSchemaRegistry:
    """The companion is the registry; the code is one implementation of it."""

    def test_every_registered_schema_is_implemented_here(self) -> None:
        """A writer missing a registered schema would version blocks the newer way and silently
        fork their identities, which is the failure canonical serialization exists to prevent
        re-entering through the version field."""
        implemented = {(kind.value, version) for kind, version in Block.registry()}
        published = {
            (memory_type, entry["schema_version"])
            for memory_type, entries in golden.registry()["schemas"].items()
            for entry in entries
        }
        assert published <= implemented, f"registered but not implemented: {published - implemented}"

    def test_this_sdk_registers_nothing_the_companion_does_not_carry(self) -> None:
        """The other direction. A schema only this SDK knows makes identity comparable only to
        itself -- the per-deployment registry the protocol forbids, arrived at by accident."""
        implemented = {(kind.value, version) for kind, version in Block.registry()}
        published = {
            (memory_type, entry["schema_version"])
            for memory_type, entries in golden.registry()["schemas"].items()
            for entry in entries
        }
        assert implemented <= published, f"implemented but unregistered: {implemented - published}"

    def test_versions_are_consecutive_from_one(self) -> None:
        """ "Oldest" is by registration number, so the numbers have to be a sequence."""
        for memory_type, entries in golden.registry()["schemas"].items():
            versions = [entry["schema_version"] for entry in entries]
            assert versions == list(range(1, len(versions) + 1)), memory_type


class TestRegisteringAnUnknownSchema:
    """Defining a schema the protocol's registry does not carry must not pass unremarked."""

    def test_it_warns_and_names_the_consequence(self, caplog) -> None:
        with caplog.at_level("WARNING"):

            class UnregisteredSemantic(SemanticBlock):
                MEMORY_TYPE = MemoryType.SEMANTIC
                SCHEMA_VERSION = 99

        assert "schema registry does not carry" in caplog.text
        assert "UnregisteredSemantic" in caplog.text
        assert golden.CORPUS_REPOSITORY in caplog.text

    def test_it_warns_rather_than_refuses(self, caplog) -> None:
        """Defining a schema is how one comes to be proposed for registration in the first place.

        An exception here would make the SDK unusable for the work that precedes registration --
        which is not the failure being guarded against. The failure is doing it silently.
        """
        with caplog.at_level("WARNING"):

            class ProposedSemantic(SemanticBlock):
                MEMORY_TYPE = MemoryType.SEMANTIC
                SCHEMA_VERSION = 98

        assert (MemoryType.SEMANTIC, 98) in Block.registry()

    def test_a_registered_schema_is_quiet(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            Block.schemas(MemoryType.SEMANTIC)
        assert "schema registry does not carry" not in caplog.text


class TestVectorsNeedNoTestFramework:
    """The vectors must be reachable on a plain install, which is what their callers have.

    Checked in subprocesses because the assertions are about a *fresh* interpreter: this suite
    has already imported the pytest-backed half, so asking about ``sys.modules`` in-process
    would answer a different question.
    """

    def _run(self, body: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-c", dedent(body)], capture_output=True, text=True, check=False)

    def test_importing_the_package_does_not_load_the_suites(self) -> None:
        # Reading four JSON files must not require a Python test framework -- the reader this
        # half of the kit exists for writes their client in another language entirely.
        result = self._run("""
            import sys
            import boltzmann.conformance
            assert "boltzmann.conformance.suite" not in sys.modules, "the suites were imported"
            assert boltzmann.conformance.golden.VECTOR_FILES
        """)
        assert result.returncode == 0, result.stderr

    def test_the_vectors_load_with_pytest_unavailable(self) -> None:
        # Simulates the plain install: pytest is not importable at all.
        result = self._run("""
            import sys
            sys.meta_path.insert(0, type("NoPytest", (), {
                "find_spec": lambda self, name, *a: None if name != "pytest" else exec(
                    'raise ModuleNotFoundError("No module named \\'pytest\\'", name="pytest")'
                ),
            })())
            from boltzmann.conformance import golden
            assert len(golden.load("block_ids.json")["vectors"]) > 0
            assert set(golden.load_all()) == set(golden.VECTOR_FILES)
        """)
        assert result.returncode == 0, result.stderr

    def test_a_suite_says_what_to_install_when_pytest_is_missing(self) -> None:
        result = self._run("""
            import sys
            sys.meta_path.insert(0, type("NoPytest", (), {
                "find_spec": lambda self, name, *a: None if name != "pytest" else exec(
                    'raise ModuleNotFoundError("No module named \\'pytest\\'", name="pytest")'
                ),
            })())
            try:
                from boltzmann.conformance import BlockStoreConformance
            except ImportError as exc:
                assert "pyboltzmann[conformance]" in str(exc), str(exc)
            else:
                raise AssertionError("expected an ImportError")
        """)
        assert result.returncode == 0, result.stderr

    def test_a_suite_still_resolves_when_pytest_is_present(self) -> None:
        from boltzmann.conformance import BlockStoreConformance, sample_semantic

        assert issubclass(BlockStoreConformance, object)
        assert sample_semantic().label

    def test_an_unknown_name_is_still_an_attribute_error(self) -> None:
        import boltzmann.conformance

        with pytest.raises(AttributeError, match="has no attribute"):
            _ = boltzmann.conformance.NotAThing  # type: ignore[attr-defined]

    def test_dir_reports_the_whole_surface(self) -> None:
        import boltzmann.conformance

        assert set(dir(boltzmann.conformance)) == set(boltzmann.conformance.__all__)
