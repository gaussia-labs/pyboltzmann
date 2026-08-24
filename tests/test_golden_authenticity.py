"""The authenticity golden vectors, replayed: another implementation must reach these verdicts.

Two files, two layers. ``sshsig.json`` pins the wire format -- an implementer whose framing is
wrong finds out at the ``signed_data_blob`` assertion, not at an opaque verify failure. And
``signatures.json`` pins the *judgement*: published chains, published key pairs, and the verdict
a verifier MUST reach, including the cases the paper works through by hand (Section 8.9).
"""

from __future__ import annotations

import base64
import subprocess
import sys
from textwrap import dedent

import pytest

from boltzmann.authenticity import SshSignature, signed_data, verify
from boltzmann.authenticity.authenticator import Authenticator, AuthorshipState
from boltzmann.authenticity.pins import PinSource, write_pin
from boltzmann.authenticity.record import SignatureRecord
from boltzmann.authenticity.trust_root import TrustRoot
from boltzmann.conformance.golden import load
from boltzmann.exceptions import (
    NamespaceMismatchError,
    SignatureFormatError,
    SignatureInvalidError,
    UnsupportedKeyTypeError,
)
from boltzmann.identity.digest import OciDigest
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.memory import MemoryBlockStore

SSHSIG = load("sshsig.json")
SIGNATURES = load("signatures.json")

EXPECTED_ERRORS = {
    "namespace_mismatch": NamespaceMismatchError,
    "format": SignatureFormatError,
    "invalid": SignatureInvalidError,
    "unsupported_key_type": UnsupportedKeyTypeError,
}


class TestSshsigVectors:
    """The framing layer, byte for byte."""

    @pytest.mark.parametrize("vector", SSHSIG["vectors"], ids=lambda vector: vector["name"])
    def test_the_published_bytes_reproduce(self, vector: dict) -> None:
        pytest.importorskip("cryptography")
        message = base64.b64decode(vector["message_base64"])
        data = signed_data(message, namespace=vector["namespace"], hash_algorithm=vector["hash_algorithm"])
        assert data.hex() == vector["signed_data_blob_hex"], "the framing disagrees before any key is involved"
        signature = SshSignature.parse(vector["armored"])
        assert signature.to_blob().hex() == vector["signature_blob_hex"]
        assert signature.signature.hex() == vector["raw_signature_hex"]
        assert (
            verify(signature, message, namespace=vector["namespace"]).fingerprint == (SSHSIG["keys"][0]["fingerprint"])
        )

    @pytest.mark.parametrize("rejection", SSHSIG["rejections"], ids=lambda rejection: rejection["name"])
    def test_the_published_rejections_reject(self, rejection: dict) -> None:
        pytest.importorskip("cryptography")
        expected = EXPECTED_ERRORS[rejection["expect"]]
        message = base64.b64decode(SSHSIG["vectors"][0]["message_base64"])

        def reject() -> None:
            if "blob_hex" in rejection:
                signature = SshSignature.from_blob(bytes.fromhex(rejection["blob_hex"]))
            else:
                signature = SshSignature.parse(rejection["armored"])
            verify(signature, message, namespace=rejection.get("verify_under", SSHSIG["namespace"]))

        with pytest.raises(expected):
            reject()


def replay(case: dict) -> tuple:
    store = MemoryBlockStore()
    for described in SIGNATURES["snapshots"].values():
        digest = store.put_bytes(described["canonical"].encode("utf-8"))
        assert str(digest) == described["digest"], "the published snapshot bytes do not hash to their digest"
    # Every published record is stored beside the chain, the way a real brain holds them: the
    # verifier re-checks ancestor revisions' quorums from the store, so a subtree whose admitting
    # revision travelled without its signatures is -- correctly -- a rejected subtree.
    from boltzmann.authenticity.record import store_record

    for described in SIGNATURES["signatures"].values():
        store_record(store, SignatureRecord.model_validate(described))
    if "pin" in case:
        pinned = SIGNATURES["trust_roots"][case["pin"]]
        write_pin(store, OciDigest.parse(pinned["digest"]), PinSource.OUT_OF_BAND)
    snapshot = Snapshot.model_validate_json(SIGNATURES["snapshots"][case["snapshot"]]["canonical"].encode("utf-8"))
    records = [SignatureRecord.model_validate(SIGNATURES["signatures"][name]) for name in case["signatures"]]
    current = None
    if "current_trust_root" in case:
        current = TrustRoot.model_validate(SIGNATURES["trust_roots"][case["current_trust_root"]]["document"])
    report = Authenticator(store).authenticate(snapshot, records=records, current=current)
    return report, case["expect"]


class TestSignatureVerdicts:
    """The judgement layer: the paper's worked cases as executable oracles."""

    @pytest.mark.parametrize("case", SIGNATURES["cases"], ids=lambda case: case["name"])
    def test_the_verifier_reaches_the_published_verdict(self, case: dict) -> None:
        pytest.importorskip("cryptography")
        report, expect = replay(case)
        assert report.state is AuthorshipState(expect["state"])
        if "role" in expect:
            assert report.role.value == expect["role"]
        if "required_scopes" in expect:
            assert [scope.value for scope in report.required_scopes] == expect["required_scopes"]
        if "quorum_required" in expect:
            assert report.quorum_required == expect["quorum_required"]
        if "quorum_met" in expect:
            assert report.quorum_met == expect["quorum_met"]
        if "pinned" in expect:
            assert report.pinned is expect["pinned"]
        for key_name, outcome in expect.get("outcomes", {}).items():
            fingerprint = next(key["fingerprint"] for key in SIGNATURES["keys"] if key["name"] == key_name)
            assert report.outcomes()[fingerprint].value == outcome
        for kind in expect.get("findings_include", []):
            assert any(finding.kind.value == kind for finding in report.findings), kind
        for key_name in expect.get("withdrawn", []):
            fingerprint = next(key["fingerprint"] for key in SIGNATURES["keys"] if key["name"] == key_name)
            assert fingerprint in [verdict.key for verdict in report.withdrawn]

    def test_every_authored_finding_kind_appears_somewhere(self) -> None:
        # Coverage pinned by name: a case list that quietly stopped exercising the interesting
        # verdicts would still pass every remaining case.
        named = {kind for case in SIGNATURES["cases"] for kind in case["expect"].get("findings_include", [])}
        assert {"quorum_failure", "compromised_key", "genesis_below_quorum"} <= named
        states = {case["expect"]["state"] for case in SIGNATURES["cases"]}
        assert states == {"authorized", "unsigned", "unauthorized"}


class TestTheExtraIsOptional:
    """Everything structural works with the cryptography package absent -- in a fresh interpreter."""

    def _run(self, body: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-c", dedent(body)], capture_output=True, text=True, check=False)

    def test_a_bare_install_parses_fingerprints_and_rejects_a_lying_record(self) -> None:
        result = self._run("""
            import sys

            class NoCryptography:
                def find_spec(self, name, *args):
                    if name == "cryptography" or name.startswith("cryptography."):
                        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                    return None

            sys.meta_path.insert(0, NoCryptography())

            import boltzmann  # the whole surface imports without the extra
            from boltzmann.authenticity import SshSignature, signature_backend_available, verify
            from boltzmann.conformance.golden import load
            from boltzmann.exceptions import VerificationUnavailableError

            assert not signature_backend_available()
            vector = load("sshsig.json")["vectors"][0]
            signature = SshSignature.parse(vector["armored"])  # framing needs no mathematics
            assert signature.fingerprint == load("sshsig.json")["keys"][0]["fingerprint"]

            # The one rejection that must keep working bare: a record whose named fingerprint and
            # embedded key disagree is internally inconsistent, no cryptography required.
            from boltzmann.authenticity.record import SignatureRecord
            record = SignatureRecord(
                snapshot="sha256:" + "0" * 64,
                key="SHA256:" + "A" * 43,
                signature=vector["armored"],
            )
            assert record.embedded_key is not None
            assert record.embedded_key.fingerprint != record.key

            import base64
            try:
                verify(signature, base64.b64decode(vector["message_base64"]))
            except VerificationUnavailableError as error:
                assert "pyboltzmann[authenticity]" in str(error)
            else:
                raise AssertionError("verify() must not succeed without the extra")
        """)
        assert result.returncode == 0, result.stderr

    def test_a_bare_verifier_reports_a_lying_fingerprint_without_the_extra(self) -> None:
        result = self._run("""
            import sys

            class NoCryptography:
                def find_spec(self, name, *args):
                    if name == "cryptography" or name.startswith("cryptography."):
                        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                    return None

            sys.meta_path.insert(0, NoCryptography())

            from boltzmann.authenticity.authenticator import Authenticator, SignatureOutcome
            from boltzmann.authenticity.record import SignatureRecord
            from boltzmann.conformance.golden import load
            from boltzmann.module.snapshot import Snapshot
            from boltzmann.store.memory import MemoryBlockStore

            vectors = load("signatures.json")
            store = MemoryBlockStore()
            for described in vectors["snapshots"].values():
                store.put_bytes(described["canonical"].encode("utf-8"))
            snapshot = Snapshot.model_validate_json(vectors["snapshots"]["S7"]["canonical"].encode())
            honest = vectors["signatures"]["A-over-S7"]
            lying = {**honest, "key": "SHA256:" + "A" * 43}
            report = Authenticator(store).authenticate(
                snapshot,
                records=[SignatureRecord.model_validate(lying), SignatureRecord.model_validate(honest)],
            )
            outcomes = [verdict.outcome for verdict in report.signatures]
            assert SignatureOutcome.FINGERPRINT_MISMATCH in outcomes, outcomes
            assert SignatureOutcome.UNVERIFIABLE in outcomes, outcomes
            assert report.state.value == "unauthorized", "unchecked must never read as authorized"
        """)
        assert result.returncode == 0, result.stderr
