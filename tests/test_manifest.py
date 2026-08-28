"""The brain manifest: what two registries must agree on to exchange a brain."""

import json

import pytest
from pydantic import ValidationError

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.distribution.manifest import BrainManifest, Descriptor, build_manifest, parse_manifest
from boltzmann.distribution.media_types import (
    ANNOTATION_MERKLE_ROOT,
    ANNOTATION_PROTOCOL_VERSION,
    ARTIFACT_TYPE,
    CONFIG_MEDIA_TYPE,
    PROJECTION_MEDIA_TYPE,
    VECTOR_INDEX_MEDIA_TYPE,
    module_media_type,
)
from boltzmann.distribution.projection import Projection
from boltzmann.exceptions import DistributionError
from boltzmann.identity.digest import MerkleRoot, OciDigest
from boltzmann.module.snapshot import ModuleRef, Snapshot


def reference(memory_type: MemoryType, embedding_model: str | None = None) -> ModuleRef:
    return ModuleRef(
        memory_type=memory_type,
        root=MerkleRoot.of(memory_type.value.encode()),
        composition=OciDigest.of(f"{memory_type.value} leaves".encode()),
        block_count=3,
        embedding_model=embedding_model,
    )


def config_descriptor() -> Descriptor:
    return Descriptor(media_type=CONFIG_MEDIA_TYPE, digest=OciDigest.of(b"snapshot"), size=128)


def build(*memory_types: MemoryType) -> tuple[Snapshot, BrainManifest]:
    references = [reference(kind) for kind in memory_types]
    snapshot = Snapshot.of(references)
    layers = [Descriptor.for_module(ref, OciDigest.of(ref.memory_type.value.encode()), 1024) for ref in references]
    return snapshot, build_manifest(snapshot, config_descriptor(), layers)


class TestDescriptor:
    """A descriptor carries both identities: the file's, and the composition's inside it."""

    def test_digest_and_merkle_root_are_separate(self) -> None:
        ref = reference(MemoryType.SEMANTIC)
        layer = Descriptor.for_module(ref, OciDigest.of(b"layer bytes"), 1024)
        assert layer.digest == OciDigest.of(b"layer bytes")
        assert layer.merkle_root == ref.root
        assert str(layer.digest) != str(layer.merkle_root)

    def test_annotations_let_a_selective_install_resolve_the_layer(self) -> None:
        layer = Descriptor.for_module(reference(MemoryType.EPISODIC), OciDigest.of(b"x"), 1)
        assert layer.memory_type is MemoryType.EPISODIC
        assert layer.media_type == module_media_type(MemoryType.EPISODIC)

    def test_embedding_model_travels_when_present(self) -> None:
        """A vector index cannot be rebuilt, so what produced it has to be recorded."""
        tagged = Descriptor.for_module(reference(MemoryType.SEMANTIC, "qwen3@1.0"), OciDigest.of(b"x"), 1)
        untagged = Descriptor.for_module(reference(MemoryType.SEMANTIC), OciDigest.of(b"x"), 1)
        assert tagged.annotations["ai.gaussia.boltzmann.embedding-model"] == "qwen3@1.0"
        assert "ai.gaussia.boltzmann.embedding-model" not in untagged.annotations

    def test_a_layer_without_annotations_has_no_module(self) -> None:
        plain = Descriptor(media_type="application/vnd.oci.image.layer.v1.tar", digest=OciDigest.of(b"x"), size=1)
        assert plain.memory_type is None
        assert plain.merkle_root is None

    def test_a_malformed_root_annotation_is_refused(self) -> None:
        layer = Descriptor(
            media_type=module_media_type(MemoryType.SEMANTIC),
            digest=OciDigest.of(b"x"),
            size=1,
            annotations={ANNOTATION_MERKLE_ROOT: "not-a-digest"},
        )
        with pytest.raises(Exception, match=r"malformed"):
            _ = layer.merkle_root

    def test_the_digest_must_be_an_oci_digest(self) -> None:
        with pytest.raises(ValidationError):
            Descriptor(media_type="x", digest=MerkleRoot.of(b"a composition"), size=1)


class TestBuildManifest:
    """A manifest must not claim a root nobody can fetch."""

    def test_one_layer_per_module(self) -> None:
        snapshot, manifest = build(MemoryType.CANONICAL, MemoryType.SEMANTIC)
        assert manifest.modules == [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        for kind in snapshot.installed:
            layer = manifest.layer_for(kind)
            assert layer is not None
            assert layer.merkle_root == snapshot.root_of(kind)

    def test_declares_the_artifact_and_protocol_version(self) -> None:
        _, manifest = build(MemoryType.SEMANTIC)
        assert manifest.artifact_type == ARTIFACT_TYPE
        assert manifest.annotations[ANNOTATION_PROTOCOL_VERSION] == str(PROTOCOL_VERSION)

    def test_a_missing_layer_is_refused(self) -> None:
        """Publishing a snapshot whose module has no blob would strand the consumer."""
        snapshot = Snapshot.of([reference(MemoryType.CANONICAL), reference(MemoryType.SEMANTIC)])
        only_one = [Descriptor.for_module(reference(MemoryType.CANONICAL), OciDigest.of(b"x"), 1)]
        with pytest.raises(DistributionError, match="carries no layer"):
            build_manifest(snapshot, config_descriptor(), only_one)

    def test_a_wrong_config_media_type_is_refused(self) -> None:
        snapshot, _ = build(MemoryType.SEMANTIC)
        wrong = Descriptor(media_type="application/json", digest=OciDigest.of(b"x"), size=1)
        with pytest.raises(DistributionError, match="config blob must be"):
            build_manifest(snapshot, wrong, [])

    def test_a_projection_config_media_type_is_accepted(self) -> None:
        snapshot = Snapshot.of([reference(MemoryType.CANONICAL), reference(MemoryType.SEMANTIC)])
        projected = snapshot.modules[MemoryType.CANONICAL]
        config = Descriptor(media_type=PROJECTION_MEDIA_TYPE, digest=OciDigest.of(b"projection"), size=1)
        layer = Descriptor.for_module(projected, OciDigest.of(b"canonical"), 1)
        manifest = build_manifest(snapshot, config, [layer], published=[projected])
        assert manifest.config.media_type == PROJECTION_MEDIA_TYPE


class TestProjectionDocument:
    """A projection is canonical, typed, and carries no snapshot-only state."""

    def test_round_trips_canonically(self) -> None:
        source = Snapshot.of([reference(MemoryType.CANONICAL), reference(MemoryType.SEMANTIC)])
        projection = Projection(
            source=source.digest,
            modules={MemoryType.CANONICAL: source.modules[MemoryType.CANONICAL]},
        )
        assert Projection.from_document(projection.canonical_bytes()) == projection
        assert projection.digest == OciDigest.of(projection.canonical_bytes())

    def test_noncanonical_bytes_are_rejected(self) -> None:
        source = Snapshot.of([reference(MemoryType.CANONICAL)])
        projection = Projection(source=source.digest, modules=source.modules)
        pretty = json.dumps(json.loads(projection.canonical_bytes()), indent=2).encode()
        with pytest.raises(DistributionError, match="not in canonical"):
            Projection.from_document(pretty)

    def test_a_module_key_cannot_disagree_with_its_reference(self) -> None:
        source = Snapshot.of([reference(MemoryType.CANONICAL)])
        with pytest.raises(ValidationError, match="module key"):
            Projection(source=source.digest, modules={MemoryType.SEMANTIC: source.modules[MemoryType.CANONICAL]})

    def test_a_partial_artifact_is_valid(self) -> None:
        """Selective installation requires that publishing one module be legitimate."""
        _, manifest = build(MemoryType.EPISODIC)
        assert manifest.modules == [MemoryType.EPISODIC]
        assert manifest.layer_for(MemoryType.SEMANTIC) is None


class TestVectorIndexLayers:
    """The one derived structure that must travel gets its own layer."""

    def test_index_layers_do_not_shadow_module_layers(self) -> None:
        ref = reference(MemoryType.SEMANTIC, "qwen3@1.0")
        snapshot = Snapshot.of([ref])
        module_layer = Descriptor.for_module(ref, OciDigest.of(b"module"), 1024)
        index_layer = Descriptor(
            media_type=VECTOR_INDEX_MEDIA_TYPE,
            digest=OciDigest.of(b"index"),
            size=512,
            annotations=dict(module_layer.annotations),
        )
        manifest = build_manifest(snapshot, config_descriptor(), [module_layer, index_layer])

        assert manifest.layer_for(MemoryType.SEMANTIC) == module_layer
        assert manifest.vector_index_for(MemoryType.SEMANTIC) == index_layer
        assert manifest.modules == [MemoryType.SEMANTIC]


class TestParseManifest:
    """A pulled manifest must be identified before it is trusted."""

    def test_round_trips(self) -> None:
        _, manifest = build(MemoryType.CANONICAL, MemoryType.SEMANTIC)
        assert parse_manifest(manifest.to_bytes()) == manifest

    def test_the_digest_is_stable(self) -> None:
        _, manifest = build(MemoryType.SEMANTIC)
        assert manifest.digest == OciDigest.of(manifest.to_bytes())
        assert parse_manifest(manifest.to_bytes()).digest == manifest.digest

    def test_duplicate_json_keys_are_refused(self) -> None:
        _, manifest = build(MemoryType.SEMANTIC)
        duplicated = manifest.to_bytes().replace(
            b'"schemaVersion":2',
            b'"schemaVersion":2,"schemaVersion":2',
        )
        with pytest.raises(DistributionError, match="duplicate JSON key"):
            parse_manifest(duplicated)

    def test_a_foreign_artifact_is_refused(self) -> None:
        document = {"artifact_type": "application/vnd.oci.image.manifest.v1+json", "config": {}, "layers": []}
        with pytest.raises(DistributionError, match="not a Boltzmann brain"):
            parse_manifest(json.dumps(document).encode())

    def test_a_future_protocol_version_is_refused(self) -> None:
        _, manifest = build(MemoryType.SEMANTIC)
        document = json.loads(manifest.to_bytes())
        document["annotations"][ANNOTATION_PROTOCOL_VERSION] = "99"
        with pytest.raises(DistributionError, match="declares protocol version"):
            parse_manifest(json.dumps(document).encode())

    @pytest.mark.parametrize("declared", ["1", "2"])
    def test_every_wire_version_this_client_implements_is_accepted(self, declared: str) -> None:
        _, manifest = build(MemoryType.SEMANTIC)
        document = json.loads(manifest.to_bytes())
        document["annotations"][ANNOTATION_PROTOCOL_VERSION] = declared
        assert parse_manifest(json.dumps(document).encode()).digest is not None

    def test_an_unreadable_protocol_version_is_refused(self) -> None:
        _, manifest = build(MemoryType.SEMANTIC)
        document = json.loads(manifest.to_bytes())
        document["annotations"][ANNOTATION_PROTOCOL_VERSION] = "banana"
        with pytest.raises(DistributionError, match="unreadable protocol version"):
            parse_manifest(json.dumps(document).encode())

    def test_an_unreadable_manifest_names_the_upgrade_path(self) -> None:
        """A manifest that passes the gate but does not fit this client's model is most likely
        from a newer SDK, and the refusal should say what to do about it."""
        _, manifest = build(MemoryType.SEMANTIC)
        document = json.loads(manifest.to_bytes())
        document["config"]["futureField"] = {"from": "a newer SDK"}
        with pytest.raises(DistributionError, match="upgrade pyboltzmann"):
            parse_manifest(json.dumps(document).encode())

    @pytest.mark.parametrize(
        ("data", "match"),
        [(b"not json", "not valid JSON"), (b"[]", "must be an object")],
    )
    def test_malformed_input_is_refused(self, data: bytes, match: str) -> None:
        with pytest.raises(DistributionError, match=match):
            parse_manifest(data)
