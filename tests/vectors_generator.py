"""Regenerates the authenticity golden vectors: sshsig.json and signatures.json.

Run from the repository root: ``python tests/vectors_generator.py``. Deterministic by
construction -- published seeds, fixed timestamps -- so a regeneration that changes a published
vector means either a bug or a new format version, never noise. Once published, a vector file
must not change.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from boltzmann.authenticity import (
    Scope,
    SshPublicKey,
    TrustedKey,
    TrustRoot,
    parse_rfc4253_signature,
    put_string,
    put_uint32,
    rfc4253_signature,
    sign,
    signed_data,
)
from boltzmann.authenticity.record import SignatureRecord
from boltzmann.authenticity.sshsig import MAGIC_PREAMBLE, message_hash
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import SNAPSHOT_NAMESPACE
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import ModuleRef, Snapshot

VECTORS = Path(__file__).parent.parent / "src" / "boltzmann" / "conformance" / "vectors"

WARNING = (
    "The private seeds below are PUBLISHED, deliberately: they exist so an implementation in any "
    "language can regenerate and extend these vectors. Never use them for anything real."
)

MESSAGE = b'{"boltzmann":1}'


class Party:
    def __init__(self, name: str, seed: bytes) -> None:
        self.name = name
        self.seed = seed
        self._private = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        line = self._private.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        self.public_key = SshPublicKey.parse(line.decode("ascii"))

    def sign_blob(self, data: bytes) -> bytes:
        return rfc4253_signature("ssh-ed25519", self._private.sign(data))

    def described(self) -> dict:
        return {
            "name": self.name,
            "type": "ssh-ed25519",
            "seed_hex": self.seed.hex(),
            "public": self.public_key.authorized_key,
            "public_key_blob_hex": self.public_key.blob.hex(),
            "fingerprint": self.public_key.fingerprint,
        }

    def entry(self, *scopes: Scope, since: int = 1, **positions) -> TrustedKey:
        return TrustedKey(key=self.public_key, scopes=tuple(scopes), since=since, **positions)

    def record(self, snapshot: Snapshot, *scopes: Scope) -> SignatureRecord:
        return SignatureRecord(
            snapshot=snapshot.digest,
            key=self.public_key.fingerprint,
            scopes=tuple(scopes),
            signature=sign(snapshot.canonical_bytes(), self).armored(),
        )


def generate_sshsig() -> dict:
    lecturer = Party("lecturer", bytes(range(32)))

    def positive(hash_algorithm: str) -> dict:
        signature = sign(MESSAGE, lecturer, hash_algorithm=hash_algorithm)
        data = signed_data(MESSAGE, namespace=SNAPSHOT_NAMESPACE, hash_algorithm=hash_algorithm)
        return {
            "name": f"ed25519_{hash_algorithm}_snapshot_namespace",
            "key": "lecturer",
            "namespace": SNAPSHOT_NAMESPACE,
            "hash_algorithm": hash_algorithm,
            "message_base64": base64.b64encode(MESSAGE).decode("ascii"),
            "message_digest_hex": message_hash(hash_algorithm, MESSAGE).hex(),
            "signed_data_blob_hex": data.hex(),
            "raw_signature_hex": signature.signature.hex(),
            "signature_blob_hex": signature.to_blob().hex(),
            "armored": signature.armored(),
        }

    good = sign(MESSAGE, lecturer)
    blob = good.to_blob()

    def sha256_signature() -> str:
        """Build generic SSHSIG/SHA-256 bytes without crossing Boltzmann's signing gate."""
        data = signed_data(MESSAGE, namespace=SNAPSHOT_NAMESPACE, hash_algorithm="sha256")
        algorithm, raw = parse_rfc4253_signature(lecturer.sign_blob(data))
        return dataclasses.replace(
            good,
            hash_algorithm="sha256",
            signature_algorithm=algorithm,
            signature=raw,
        ).armored()

    def poisoned_reserved() -> str:
        data = (
            MAGIC_PREAMBLE
            + put_string(SNAPSHOT_NAMESPACE.encode())
            + put_string(b"x")
            + put_string(b"sha512")
            + put_string(message_hash("sha512", MESSAGE))
        )
        algorithm, raw = good.signature_algorithm, None
        framed = lecturer.sign_blob(data)
        algorithm, raw = parse_rfc4253_signature(framed)
        forged = dataclasses.replace(good, reserved=b"x", signature=raw, signature_algorithm=algorithm)
        return forged.armored()

    rejections = [
        {
            "name": "another_namespace_is_a_mismatch_not_a_forgery",
            "armored": sign(MESSAGE, lecturer, namespace="git").armored(),
            "verify_under": SNAPSHOT_NAMESPACE,
            "expect": "namespace_mismatch",
        },
        {
            "name": "version_2_is_refused_rather_than_read",
            "blob_hex": (blob[:6] + put_uint32(2) + blob[10:]).hex(),
            "expect": "format",
        },
        {
            "name": "a_signature_made_over_a_non_empty_reserved_never_verifies",
            "armored": poisoned_reserved(),
            "verify_under": SNAPSHOT_NAMESPACE,
            "expect": "invalid",
        },
        {
            "name": "trailing_bytes_after_the_structure",
            "blob_hex": (blob + b"\x00").hex(),
            "expect": "format",
        },
        {
            "name": "every_truncation_is_a_format_error",
            "blob_hex": blob[: len(blob) // 2].hex(),
            "expect": "format",
        },
        {
            "name": "sha256_is_valid_sshsig_but_not_a_boltzmann_signature",
            "armored": sha256_signature(),
            "expect": "format",
        },
        {
            "name": "md5_is_not_a_hash_algorithm_here",
            "blob_hex": blob.replace(put_string(b"sha512"), put_string(b"md5")).hex(),
            "expect": "format",
        },
        {
            "name": "armor_missing_its_footer",
            "armored": good.armored().replace("-----END SSH SIGNATURE-----", ""),
            "expect": "format",
        },
        {
            "name": "armor_with_a_leading_byte",
            "armored": " " + good.armored(),
            "expect": "format",
        },
    ]

    return {
        "boltzmann": 1,
        "serialization": "jcs/1",
        "hash": "sha256",
        "namespace": SNAPSHOT_NAMESPACE,
        "description": (
            "SSHSIG framing vectors. The signed_data blob is the highest-value entry: without it, a "
            "framing bug and a signing bug are indistinguishable. Note the signed data carries no "
            "version field, the armor wraps at 70 columns, and reserved is always empty in the "
            "signed data even though the outer blob's is ignored."
        ),
        "warning": WARNING,
        "keys": [lecturer.described()],
        "vectors": [positive("sha512")],
        "rejections": rejections,
    }


def semantic_reference(*labels: str) -> tuple[ModuleRef, Composition]:
    composition = Composition(MemoryType.SEMANTIC, [BlockId.of(label.encode()) for label in labels])
    reference = ModuleRef(
        memory_type=composition.memory_type,
        root=composition.root,
        composition=OciDigest.of(composition.document()),
        block_count=len(composition),
    )
    return reference, composition


def generate_signatures() -> dict:
    alice = Party("A", bytes([0xA1]) * 32)
    bob = Party("B", bytes([0xB2]) * 32)
    carol = Party("C", bytes([0xC3]) * 32)

    revision_one = TrustRoot(
        revision=1,
        govern_quorum=2,
        keys=(
            alice.entry(Scope.INGEST, Scope.COMMIT, Scope.DROP_CANONICAL, Scope.GOVERN),
            bob.entry(Scope.INGEST, Scope.COMMIT, Scope.GOVERN),
        ),
    )
    revision_two = TrustRoot(
        revision=2,
        govern_quorum=2,
        keys=(*revision_one.keys, carol.entry(Scope.COMMIT, since=2)),
    )
    revision_three = TrustRoot(
        revision=3,
        govern_quorum=2,
        keys=(
            alice.entry(Scope.INGEST, Scope.COMMIT, Scope.DROP_CANONICAL, Scope.GOVERN),
            bob.entry(Scope.INGEST, Scope.COMMIT, Scope.GOVERN, retired_from=3),
            carol.entry(Scope.COMMIT, since=2),
        ),
    )
    liar_root = TrustRoot(
        revision=2,
        govern_quorum=2,
        keys=(*revision_one.keys, carol.entry(Scope.COMMIT, since=1)),
    )

    reference_v1, _ = semantic_reference("concept one")
    reference_v2, _ = semantic_reference("concept one", "concept two")
    reference_v3, _ = semantic_reference("concept one", "concept two", "concept three")

    genesis = Snapshot(created_at="2026-07-01T00:00:00Z", trust_root=revision_one)
    seven = Snapshot(
        created_at="2026-07-02T00:00:00Z",
        modules={reference_v1.memory_type: reference_v1},
        parents=[genesis.digest],
        trust_root=revision_one,
    )
    eight = Snapshot(
        created_at="2026-07-03T00:00:00Z",
        modules=seven.modules,
        parents=[seven.digest],
        trust_root=revision_two,
    )
    nine = Snapshot(
        created_at="2026-07-04T00:00:00Z",
        modules={reference_v2.memory_type: reference_v2},
        parents=[eight.digest],
        trust_root=revision_two,
    )
    liar_eight = Snapshot(
        created_at="2026-07-03T00:00:00Z",
        modules=seven.modules,
        parents=[seven.digest],
        trust_root=liar_root,
    )
    liar_nine = Snapshot(
        created_at="2026-07-04T00:00:00Z",
        modules={reference_v2.memory_type: reference_v2},
        parents=[liar_eight.digest],
        trust_root=liar_root,
    )
    ten = Snapshot(
        created_at="2026-07-05T00:00:00Z",
        modules=nine.modules,
        parents=[nine.digest],
        trust_root=revision_three,
    )
    eleven = Snapshot(
        created_at="2026-07-06T00:00:00Z",
        modules={reference_v3.memory_type: reference_v3},
        parents=[ten.digest],
        trust_root=revision_three,
    )
    # The compromise chain: C compromised from `nine` onward, recorded by a later revision.
    compromised_root = TrustRoot(
        revision=3,
        govern_quorum=2,
        keys=(
            alice.entry(Scope.INGEST, Scope.COMMIT, Scope.DROP_CANONICAL, Scope.GOVERN),
            bob.entry(Scope.INGEST, Scope.COMMIT, Scope.GOVERN),
            carol.entry(Scope.COMMIT, since=2, compromised_from=nine.digest),
        ),
    )
    twelve = Snapshot(
        created_at="2026-07-06T00:00:00Z",
        modules=nine.modules,
        parents=[nine.digest],
        trust_root=compromised_root,
    )

    snapshots = {
        "genesis": genesis,
        "S7": seven,
        "S8": eight,
        "S9": nine,
        "S8-self-admitted": liar_eight,
        "S9-on-a-lie": liar_nine,
        "S10-retires-B": ten,
        "S11": eleven,
        "S12-records-compromise": twelve,
    }

    records = {
        "A-over-genesis": alice.record(genesis, Scope.GOVERN),
        "B-over-genesis": bob.record(genesis, Scope.GOVERN),
        "A-over-S7": alice.record(seven, Scope.COMMIT),
        "A-over-S8": alice.record(eight, Scope.GOVERN),
        "B-over-S8": bob.record(eight, Scope.GOVERN),
        "C-over-S9": carol.record(nine, Scope.COMMIT),
        "C-over-S8-self-admitted": carol.record(liar_eight, Scope.GOVERN),
        "C-over-S9-on-a-lie": carol.record(liar_nine, Scope.COMMIT),
        "A-over-S10": alice.record(ten, Scope.GOVERN),
        "B-over-S10": bob.record(ten, Scope.GOVERN),
        "B-over-S11": bob.record(eleven, Scope.COMMIT),
        "A-over-S11": alice.record(eleven, Scope.COMMIT),
        "A-over-S12": alice.record(twelve, Scope.GOVERN),
        "B-over-S12": bob.record(twelve, Scope.GOVERN),
    }

    cases = [
        {
            "name": "a_genesis_is_exempt_from_the_quorum_rule",
            "snapshot": "genesis",
            "signatures": ["A-over-genesis", "B-over-genesis"],
            "expect": {"state": "authorized", "role": "genesis", "required_scopes": ["govern"]},
        },
        {
            "name": "a_genesis_below_its_own_declared_quorum_warns_without_blocking",
            "snapshot": "genesis",
            "signatures": ["A-over-genesis"],
            "expect": {"state": "authorized", "findings_include": ["genesis_below_quorum"]},
        },
        {
            "name": "an_ordinary_commit_verifies_against_the_trust_root_in_force",
            "snapshot": "S7",
            "signatures": ["A-over-S7"],
            "expect": {"state": "authorized", "role": "ordinary", "required_scopes": ["commit"]},
        },
        {
            "name": "an_unsigned_snapshot_is_unsigned_not_invalid",
            "snapshot": "S7",
            "signatures": [],
            "expect": {"state": "unsigned"},
        },
        {
            "name": "admitting_an_owner_by_quorum",
            "snapshot": "S8",
            "signatures": ["A-over-S8", "B-over-S8"],
            "expect": {
                "state": "authorized",
                "role": "revision",
                "quorum_required": 2,
                "quorum_met": 2,
                "required_scopes": ["govern"],
            },
        },
        {
            "name": "the_admitted_key_commits_ordinarily",
            "snapshot": "S9",
            "signatures": ["C-over-S9"],
            "expect": {"state": "authorized", "outcomes": {"C": "valid"}},
        },
        {
            "name": "a_key_admitting_itself_fails_with_no_pin_at_all",
            "snapshot": "S8-self-admitted",
            "signatures": ["C-over-S8-self-admitted"],
            "expect": {
                "state": "unauthorized",
                "outcomes": {"C": "unauthorized_key"},
                "findings_include": ["quorum_failure"],
                "quorum_met": 0,
            },
        },
        {
            "name": "rejection_propagates_to_the_forged_subtree",
            "snapshot": "S9-on-a-lie",
            "signatures": ["C-over-S9-on-a-lie"],
            "expect": {"state": "unauthorized", "findings_include": ["quorum_failure"]},
        },
        {
            "name": "a_retired_key_counts_toward_the_quorum_that_retired_it",
            "snapshot": "S10-retires-B",
            "signatures": ["A-over-S10", "B-over-S10"],
            "expect": {"state": "authorized", "quorum_met": 2},
        },
        {
            "name": "a_retired_keys_later_signature_fails_as_retired_not_unauthorized",
            "snapshot": "S11",
            "signatures": ["B-over-S11", "A-over-S11"],
            "expect": {"state": "authorized", "outcomes": {"B": "retired_key", "A": "valid"}},
        },
        {
            "name": "signatures_before_a_retirement_stand",
            "snapshot": "S8",
            "signatures": ["A-over-S8", "B-over-S8"],
            "expect": {"state": "authorized"},
        },
        {
            "name": "a_compromise_withdraws_from_its_position_onward",
            "snapshot": "S9",
            "signatures": ["C-over-S9"],
            "current_trust_root": "compromised",
            "expect": {
                "state": "unauthorized",
                "withdrawn": ["C"],
                "findings_include": ["compromised_key"],
            },
        },
        {
            "name": "a_pinned_trust_root_rejects_a_stranger",
            "snapshot": "S8-self-admitted",
            "signatures": ["C-over-S8-self-admitted"],
            "pin": "revision-one",
            "expect": {"state": "unauthorized", "findings_include": ["quorum_failure"]},
        },
        {
            "name": "a_pin_is_satisfied_through_approved_revisions",
            "snapshot": "S9",
            "signatures": ["C-over-S9"],
            "pin": "revision-one",
            "expect": {"state": "authorized", "pinned": True},
        },
    ]

    return {
        "boltzmann": 1,
        "serialization": "jcs/1",
        "hash": "sha256",
        "namespace": SNAPSHOT_NAMESPACE,
        "description": (
            "Snapshots, published test key pairs, and the verdict a verifier MUST reach. Before a "
            "case runs, every document in `snapshots` and every record in `signatures` is stored "
            "beside the chain, the way a real brain holds them -- ancestor revisions are re-judged "
            "from storage, so a subtree whose admitting revision lacks its quorum falls with it. A "
            "case then presents the named records over its snapshot, optionally a pinned trust root "
            "and the newest trust root the verifier knows, and states the verdict."
        ),
        "warning": WARNING,
        "keys": [alice.described(), bob.described(), carol.described()],
        "trust_roots": {
            "revision-one": {
                "document": json.loads(revision_one.canonical_bytes()),
                "digest": str(revision_one.digest),
            },
            "revision-two": {
                "document": json.loads(revision_two.canonical_bytes()),
                "digest": str(revision_two.digest),
            },
            "compromised": {
                "document": json.loads(compromised_root.canonical_bytes()),
                "digest": str(compromised_root.digest),
            },
        },
        "snapshots": {
            name: {"canonical": snapshot.canonical_bytes().decode("utf-8"), "digest": str(snapshot.digest)}
            for name, snapshot in snapshots.items()
        },
        "signatures": {name: json.loads(record.canonical_bytes()) for name, record in records.items()},
        "cases": cases,
    }


def main() -> None:
    for name, document in (("sshsig.json", generate_sshsig()), ("signatures.json", generate_signatures())):
        path = VECTORS / name
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
