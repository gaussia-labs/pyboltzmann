"""Interoperability against OpenSSH itself: the reference implementation is the oracle.

Golden vectors pin our own behaviour; these tests pin the seam between implementations, in both
directions: a signature this SDK writes must be accepted by ``ssh-keygen -Y verify``, and a
signature ``ssh-keygen -Y sign`` writes must verify here. Comparison across signers is always by
verification, never by byte equality -- except for the one case where byte equality is the
point: both sides emit deterministic Ed25519 and the same 70-column armor.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from boltzmann.authenticity import SshPublicKey, SshSignature, rfc4253_signature, sign, verify
from boltzmann.constants import SNAPSHOT_NAMESPACE
from boltzmann.exceptions import SignatureInvalidError

requires_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="interoperability against OpenSSH needs ssh-keygen on PATH",
)

MESSAGE = b'{"boltzmann":1}'
PRINCIPAL = "boltzmann@example.invalid"
"""Synthesized for ssh-keygen only. A trust root carries no principals; ``allowed_signers`` is
OpenSSH's own trust format and must never be mistaken for the trust root's."""


def _ssh_keygen(*args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["ssh-keygen", *args], input=stdin, capture_output=True, check=False)


@pytest.fixture
def keypair(tmp_path: Path) -> tuple[Path, SshPublicKey, object]:
    """A throwaway Ed25519 key pair written the way OpenSSH reads one."""
    ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    key_path = tmp_path / "key"
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        )
    )
    key_path.chmod(0o600)
    line = (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode("ascii")
    )
    (tmp_path / "key.pub").write_text(line + "\n")
    return key_path, SshPublicKey.parse(line), private


class _Signer:
    def __init__(self, public_key: SshPublicKey, private: object) -> None:
        self.public_key = public_key
        self._private = private

    def sign_blob(self, data: bytes) -> bytes:
        return rfc4253_signature("ssh-ed25519", self._private.sign(data))  # type: ignore[attr-defined]


@requires_ssh_keygen
class TestOursUnderOpenssh:
    """Signatures this SDK writes are accepted by the reference implementation."""

    def test_check_novalidate_accepts_our_signature(self, keypair: tuple, tmp_path: Path) -> None:
        _, public, private = keypair
        signature_path = tmp_path / "ours.sig"
        signature_path.write_text(sign(MESSAGE, _Signer(public, private)).armored())
        result = _ssh_keygen(
            "-Y", "check-novalidate", "-n", SNAPSHOT_NAMESPACE, "-s", str(signature_path), stdin=MESSAGE
        )
        assert result.returncode == 0, result.stderr.decode()

    def test_check_novalidate_rejects_our_signature_under_another_namespace(
        self, keypair: tuple, tmp_path: Path
    ) -> None:
        _, public, private = keypair
        signature_path = tmp_path / "ours.sig"
        signature_path.write_text(sign(MESSAGE, _Signer(public, private)).armored())
        result = _ssh_keygen("-Y", "check-novalidate", "-n", "git", "-s", str(signature_path), stdin=MESSAGE)
        assert result.returncode != 0

    def test_verify_with_allowed_signers_accepts_our_signature(self, keypair: tuple, tmp_path: Path) -> None:
        _, public, private = keypair
        signature_path = tmp_path / "ours.sig"
        signature_path.write_text(sign(MESSAGE, _Signer(public, private)).armored())
        allowed = tmp_path / "allowed_signers"
        allowed.write_text(f"{PRINCIPAL} {public.authorized_key}\n")
        result = _ssh_keygen(
            "-Y",
            "verify",
            "-f",
            str(allowed),
            "-I",
            PRINCIPAL,
            "-n",
            SNAPSHOT_NAMESPACE,
            "-s",
            str(signature_path),
            stdin=MESSAGE,
        )
        assert result.returncode == 0, result.stderr.decode()

    def test_a_non_empty_outer_reserved_is_accepted_by_openssh(self, keypair: tuple, tmp_path: Path) -> None:
        import dataclasses

        from boltzmann.authenticity import armor

        _, public, private = keypair
        signature = dataclasses.replace(sign(MESSAGE, _Signer(public, private)), reserved=b"x")
        signature_path = tmp_path / "outer.sig"
        signature_path.write_text(armor(signature.to_blob()))
        result = _ssh_keygen(
            "-Y", "check-novalidate", "-n", SNAPSHOT_NAMESPACE, "-s", str(signature_path), stdin=MESSAGE
        )
        assert result.returncode == 0, result.stderr.decode()


@requires_ssh_keygen
class TestTheirsUnderUs:
    """Signatures the reference implementation writes verify here."""

    def test_an_openssh_signature_verifies_and_defaults_to_sha512(self, keypair: tuple) -> None:
        key_path, public, _ = keypair
        result = _ssh_keygen("-Y", "sign", "-f", str(key_path), "-n", SNAPSHOT_NAMESPACE, stdin=MESSAGE)
        assert result.returncode == 0, result.stderr.decode()
        signature = SshSignature.parse(result.stdout.decode("ascii"))
        assert signature.hash_algorithm == "sha512"
        assert verify(signature, MESSAGE).matches(public)

    def test_an_openssh_sha256_signature_also_verifies(self, keypair: tuple) -> None:
        key_path, public, _ = keypair
        result = _ssh_keygen(
            "-Y", "sign", "-f", str(key_path), "-n", SNAPSHOT_NAMESPACE, "-O", "hashalg=sha256", stdin=MESSAGE
        )
        assert result.returncode == 0, result.stderr.decode()
        signature = SshSignature.parse(result.stdout.decode("ascii"))
        assert signature.hash_algorithm == "sha256"
        assert verify(signature, MESSAGE).matches(public)

    def test_an_openssh_signature_over_other_bytes_is_a_forgery_here(self, keypair: tuple) -> None:
        key_path, _, _ = keypair
        result = _ssh_keygen("-Y", "sign", "-f", str(key_path), "-n", SNAPSHOT_NAMESPACE, stdin=MESSAGE)
        signature = SshSignature.parse(result.stdout.decode("ascii"))
        with pytest.raises(SignatureInvalidError):
            verify(signature, MESSAGE + b" ")

    def test_both_sides_emit_byte_identical_armor(self, keypair: tuple) -> None:
        # Deterministic Ed25519 plus the same 70-column wrap: the one place byte equality is
        # legitimate across implementations. A courtesy, not a requirement -- the parser accepts
        # any wrap -- but it pins that our writer matches sshbuf_dtob64 exactly.
        key_path, public, private = keypair
        result = _ssh_keygen("-Y", "sign", "-f", str(key_path), "-n", SNAPSHOT_NAMESPACE, stdin=MESSAGE)
        assert result.stdout.decode("ascii") == sign(MESSAGE, _Signer(public, private)).armored()

    def test_fingerprints_agree_with_ssh_keygen(self, keypair: tuple, tmp_path: Path) -> None:
        _, public, _ = keypair
        result = _ssh_keygen("-lf", str(tmp_path / "key.pub"))
        assert result.returncode == 0, result.stderr.decode()
        assert public.fingerprint == result.stdout.split()[1].decode("ascii")
