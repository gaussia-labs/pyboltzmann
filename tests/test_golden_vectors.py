"""The golden vectors must keep describing this implementation.

These files ship in the wheel so an implementation in another language can check itself against
the same cases. Running them here is what makes them trustworthy: if a change to the kernel would
alter an identity, this test fails before the vectors go stale.

A failure here means one of two things. Either the change is a bug, or it is a deliberate change to
the serialization -- in which case it needs a new serialization identifier, not a regenerated file.
"""

import subprocess
import sys
from textwrap import dedent

import pytest

from boltzmann.blocks.base import Block
from boltzmann.conformance import golden
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.identity.digest import BlockId
from boltzmann.identity.serialization import SERIALIZATION_ID, canonicalize
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
        assert vectors["vectors"]

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
        """The split point of RFC 6962 is where an off-by-one would hide."""
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

    def test_every_leaf_of_the_vector_tree_is_covered(self) -> None:
        vectors = golden.load("inclusion_proofs.json")["vectors"]
        assert {vector["leaf_index"] for vector in vectors} == set(range(vectors[0]["tree_size"]))


class TestSerializationVectors:
    """Canonicalization must still produce the published bytes."""

    @pytest.mark.parametrize("vector", golden.load("serialization.json")["vectors"], ids=lambda v: v["name"])
    def test_value_canonicalizes_to_the_published_bytes(self, vector: dict) -> None:
        assert canonicalize(vector["value"]).decode() == vector["canonical_bytes"]

    @pytest.mark.parametrize("vector", golden.load("serialization.json")["vectors"], ids=lambda v: v["name"])
    def test_bytes_hash_to_the_published_digest(self, vector: dict) -> None:
        assert str(BlockId.of(vector["canonical_bytes"].encode())) == vector["sha256"]

    def test_the_safe_integer_boundary_is_covered(self) -> None:
        names = {vector["name"] for vector in golden.load("serialization.json")["vectors"]}
        assert "safe_integer_bounds" in names


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
