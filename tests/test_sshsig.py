"""The SSHSIG layer: framing is checked before mathematics, and every malformation is typed.

A signature is attacker-controlled input, so the properties defended here are structural: a
declared length is never trusted enough to allocate against, a truncated blob is a typed error
and never short data, and the traps verified against OpenSSH 10.2 -- no version field in the
signed data, the ``reserved`` asymmetry, the 70-column armor -- are pinned so a refactor cannot
silently trade interoperability away.
"""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from boltzmann.authenticity import (
    ARMOR_WRAP,
    MAX_STRING,
    SshPublicKey,
    SshSignature,
    WireReader,
    armor,
    dearmor,
    normalized,
    put_string,
    put_uint32,
    rfc4253_signature,
    sign,
    signed_data,
    verify,
)
from boltzmann.constants import SNAPSHOT_NAMESPACE
from boltzmann.exceptions import (
    NamespaceMismatchError,
    SignatureFormatError,
    SignatureInvalidError,
    UnsupportedKeyTypeError,
    WeakKeyError,
)

# The published test key: seed bytes(range(32)), deliberately non-secret. Never use it for
# anything real. The derived values below were verified against ssh-keygen from OpenSSH 10.2.
SEED = bytes(range(32))
AUTHORIZED_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAOhB7/zzhC+HXDdGOdLwJln5NYwm6UNXx3chmQSVTG4"
FINGERPRINT = "SHA256:lbmsoA0yIEcEiVDRnMWuzm+nV+3ZEEpVIURqFoeSspg"
MESSAGE = b'{"boltzmann":1}'
ED25519_FIELD = (1 << 255) - 19
ED25519_ORDER = (1 << 252) + 27742317777372353535851937790883648493
ARMORED = (
    "-----BEGIN SSH SIGNATURE-----\n"
    "U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgA6EHv/POEL4dcN0Y50vAmWfk1j\n"
    "CbpQ1fHdyGZBJVMbgAAAAVYm9sdHptYW5uLnNuYXBzaG90LnYxAAAAAAAAAAZzaGE1MTIA\n"
    "AABTAAAAC3NzaC1lZDI1NTE5AAAAQJnZc/mcy6ZwA5HIlAYBNcaNwcx1uCr8/fLkh1RLMa\n"
    "C+o4VIp3u/VZ190us6wmPnKTFEog4G6GPyLceMGdKmSA0=\n"
    "-----END SSH SIGNATURE-----\n"
)


class SeededSigner:
    """A test-only signer over a published seed. The SDK itself ships no key-holding signer."""

    def __init__(self, seed: bytes) -> None:
        ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
        serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
        self._private = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        line = self._private.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        self.public_key = SshPublicKey.parse(line.decode("ascii"))

    def sign_blob(self, data: bytes) -> bytes:
        return rfc4253_signature("ssh-ed25519", self._private.sign(data))


@pytest.fixture
def signer() -> SeededSigner:
    return SeededSigner(SEED)


class TestWire:
    """The SSH wire codec never returns short data and never trusts a declared length."""

    def test_a_string_round_trips(self) -> None:
        reader = WireReader(put_string(b"boltzmann"))
        assert reader.string() == b"boltzmann"
        reader.finish()

    @given(st.binary(max_size=256))
    def test_any_bytes_round_trip_through_string_framing(self, payload: bytes) -> None:
        reader = WireReader(put_string(payload))
        assert reader.string() == payload
        reader.finish()

    @given(st.integers(min_value=0, max_value=(1 << 32) - 1))
    def test_any_uint32_round_trips(self, value: int) -> None:
        reader = WireReader(put_uint32(value))
        assert reader.uint32() == value
        reader.finish()

    @pytest.mark.parametrize("value", [-1, 1 << 32])
    def test_a_value_outside_uint32_is_refused(self, value: int) -> None:
        with pytest.raises(SignatureFormatError):
            put_uint32(value)

    def test_a_length_prefix_is_not_a_memory_allocation_request(self) -> None:
        oversized = put_uint32(MAX_STRING + 1) + b"\x00"
        with pytest.raises(SignatureFormatError, match="allocation request"):
            WireReader(oversized).string()

    def test_a_truncated_string_is_an_error_and_never_short_data(self) -> None:
        with pytest.raises(SignatureFormatError, match="truncated"):
            WireReader(put_uint32(10) + b"short").string()

    def test_trailing_bytes_are_rejected(self) -> None:
        reader = WireReader(put_string(b"x") + b"\x00")
        reader.string()
        with pytest.raises(SignatureFormatError, match="trailing"):
            reader.finish()


class TestKeys:
    """A trust-root key entry has exactly one spelling, and the fingerprint hashes the blob."""

    def test_the_published_key_fingerprints_to_the_verified_value(self) -> None:
        assert SshPublicKey.parse(AUTHORIZED_KEY).fingerprint == FINGERPRINT

    def test_the_authorized_key_form_round_trips_byte_exactly(self) -> None:
        assert SshPublicKey.parse(AUTHORIZED_KEY).authorized_key == AUTHORIZED_KEY

    def test_a_blob_round_trips_through_from_blob(self) -> None:
        key = SshPublicKey.parse(AUTHORIZED_KEY)
        again = SshPublicKey.from_blob(key.blob)
        assert again.matches(key)
        assert again.key_type == "ssh-ed25519"

    @pytest.mark.parametrize(
        "line",
        [
            AUTHORIZED_KEY + " alice@example",  # a comment is a second spelling of the same key
            'command="x" ' + AUTHORIZED_KEY,  # an options prefix is not part of a trust root entry
            AUTHORIZED_KEY.replace(" ", "  "),  # doubled separator
            "ssh-ed25519",  # no key material at all
        ],
    )
    def test_anything_but_the_canonical_two_field_form_is_rejected(self, line: str) -> None:
        with pytest.raises(SignatureFormatError):
            SshPublicKey.parse(line)

    def test_a_type_name_that_disagrees_with_the_blob_is_rejected(self) -> None:
        _, encoded = AUTHORIZED_KEY.split(" ")
        with pytest.raises(SignatureFormatError, match="encodes"):
            SshPublicKey.parse(f"ssh-rsa {encoded}")

    def test_an_ed25519_key_of_the_wrong_length_is_rejected(self) -> None:
        blob = put_string(b"ssh-ed25519") + put_string(b"\x00" * 31)
        with pytest.raises(SignatureFormatError, match="32 bytes"):
            SshPublicKey.from_blob(blob)

    def test_key_data_is_the_raw_point(self) -> None:
        key = SshPublicKey.parse(AUTHORIZED_KEY)
        assert len(key.key_data) == 32
        assert key.is_ed25519
        assert key.is_supported

    def test_a_foreign_key_type_parses_but_is_not_supported(self) -> None:
        blob = put_string(b"ssh-rsa") + put_string(b"\x01\x00\x01") + put_string(b"\x00" * 64)
        key = SshPublicKey.from_blob(blob)
        assert key.key_type == "ssh-rsa"
        assert not key.is_supported


class TestArmor:
    """The armor wraps at 70 columns on the way out and accepts anything on the way in."""

    @given(st.binary(max_size=512))
    def test_any_blob_round_trips_through_the_armor(self, blob: bytes) -> None:
        assert dearmor(armor(blob)) == blob

    @given(st.binary(min_size=1, max_size=512))
    def test_no_emitted_line_exceeds_the_openssh_wrap(self, blob: bytes) -> None:
        assert all(len(line) <= ARMOR_WRAP for line in armor(blob).splitlines())

    def test_an_unwrapped_single_line_body_is_accepted(self) -> None:
        blob = dearmor(ARMORED)
        body = "".join(ARMORED.splitlines()[1:-1])
        single = f"-----BEGIN SSH SIGNATURE-----\n{body}\n-----END SSH SIGNATURE-----\n"
        assert dearmor(single) == blob

    def test_crlf_line_endings_are_accepted(self) -> None:
        assert dearmor(ARMORED.replace("\n", "\r\n")) == dearmor(ARMORED)

    def test_the_begin_marker_must_sit_at_offset_zero(self) -> None:
        with pytest.raises(SignatureFormatError, match="offset zero"):
            dearmor(" " + ARMORED)

    def test_a_missing_footer_is_rejected(self) -> None:
        with pytest.raises(SignatureFormatError, match="footer"):
            dearmor(ARMORED.replace("-----END SSH SIGNATURE-----", ""))

    def test_an_oversized_armored_input_is_refused_before_decoding(self) -> None:
        with pytest.raises(SignatureFormatError, match="cap"):
            dearmor("-----BEGIN SSH SIGNATURE-----\n" + "A" * 9000)


class TestBlobParsing:
    """Every malformation of the outer blob is a typed, distinguishable rejection."""

    def test_the_fixture_signature_parses(self) -> None:
        signature = SshSignature.parse(ARMORED)
        assert signature.namespace == SNAPSHOT_NAMESPACE
        assert signature.hash_algorithm == "sha512"
        assert signature.public_key.fingerprint == FINGERPRINT
        assert len(signature.signature) == 64

    def test_the_blob_round_trips(self) -> None:
        signature = SshSignature.parse(ARMORED)
        assert SshSignature.from_blob(signature.to_blob()) == signature
        assert signature.armored() == ARMORED

    @given(st.data())
    def test_every_truncation_of_a_valid_blob_is_a_typed_error(self, data: st.DataObject) -> None:
        blob = SshSignature.parse(ARMORED).to_blob()
        cut = data.draw(st.integers(min_value=0, max_value=len(blob) - 1))
        with pytest.raises(SignatureFormatError):
            SshSignature.from_blob(blob[:cut])

    def test_a_wrong_preamble_is_rejected(self) -> None:
        blob = SshSignature.parse(ARMORED).to_blob()
        with pytest.raises(SignatureFormatError, match="preamble"):
            SshSignature.from_blob(b"SSHSIF" + blob[6:])

    def test_a_later_version_is_refused_rather_than_read(self) -> None:
        blob = SshSignature.parse(ARMORED).to_blob()
        with pytest.raises(SignatureFormatError, match="version 2"):
            SshSignature.from_blob(blob[:6] + put_uint32(2) + blob[10:])

    def test_trailing_bytes_after_the_structure_are_rejected(self) -> None:
        blob = SshSignature.parse(ARMORED).to_blob()
        with pytest.raises(SignatureFormatError, match="trailing"):
            SshSignature.from_blob(blob + b"\x00")

    def test_a_disallowed_hash_algorithm_is_rejected_by_name(self) -> None:
        signature = SshSignature.parse(ARMORED)
        forged = dataclasses.replace(signature, hash_algorithm="md5")
        # to_blob writes whatever it is given; the parser is the gate.
        blob = SshSignature.to_blob(forged)
        with pytest.raises(SignatureFormatError, match="md5"):
            SshSignature.from_blob(blob)

    def test_sha256_is_rejected_even_though_generic_sshsig_allows_it(self) -> None:
        signature = dataclasses.replace(SshSignature.parse(ARMORED), hash_algorithm="sha256")
        with pytest.raises(SignatureFormatError, match="sha256"):
            SshSignature.from_blob(signature.to_blob())

    def test_an_empty_namespace_is_rejected(self) -> None:
        with pytest.raises(SignatureFormatError, match="empty"):
            signed_data(MESSAGE, namespace="")


class TestVerification:
    """Verification binds the namespace, the embedded key, and the exact bytes -- nothing else."""

    def test_the_fixture_signature_verifies_and_returns_the_embedded_key(self) -> None:
        pytest.importorskip("cryptography")
        key = verify(SshSignature.parse(ARMORED), MESSAGE)
        assert key.fingerprint == FINGERPRINT

    def test_a_mutated_message_is_a_forgery(self) -> None:
        pytest.importorskip("cryptography")
        with pytest.raises(SignatureInvalidError):
            verify(SshSignature.parse(ARMORED), MESSAGE + b" ")

    def test_another_namespace_is_a_mismatch_not_a_forgery(self) -> None:
        with pytest.raises(NamespaceMismatchError, match="genuine"):
            verify(SshSignature.parse(ARMORED), MESSAGE, namespace="git")

    def test_an_in_memory_sha256_signature_is_also_rejected(self) -> None:
        signature = dataclasses.replace(SshSignature.parse(ARMORED), hash_algorithm="sha256")
        with pytest.raises(SignatureFormatError, match="sha256"):
            verify(signature, MESSAGE)

    def test_a_strong_but_unsupported_key_type_is_named_not_called_invalid(self) -> None:
        modulus = b"\x00\x80" + (b"\x00" * 383)
        blob = put_string(b"ssh-rsa") + put_string(b"\x01\x00\x01") + put_string(modulus)
        signature = dataclasses.replace(
            SshSignature.parse(ARMORED),
            public_key=SshPublicKey.from_blob(blob),
            signature_algorithm="rsa-sha2-512",
        )
        with pytest.raises(UnsupportedKeyTypeError, match="too narrow"):
            verify(signature, MESSAGE)

    @pytest.mark.parametrize(
        ("key_type", "fields", "detail"),
        [
            (b"ssh-dss", (b"\x01",) * 4, "ssh-dss"),
            (
                b"ssh-rsa",
                (b"\x01\x00\x01", b"\x00\x80" + (b"\x00" * 255)),
                "2048-bit RSA",
            ),
        ],
    )
    def test_a_key_below_the_security_floor_is_distinguishable(
        self, key_type: bytes, fields: tuple[bytes, ...], detail: str
    ) -> None:
        blob = put_string(key_type) + b"".join(put_string(field) for field in fields)
        signature = dataclasses.replace(
            SshSignature.parse(ARMORED),
            public_key=SshPublicKey.from_blob(blob),
            signature_algorithm=key_type.decode("ascii"),
        )
        with pytest.raises(WeakKeyError, match=detail):
            verify(signature, MESSAGE)

    def test_a_signature_algorithm_that_disagrees_with_the_key_is_malformed(self) -> None:
        signature = dataclasses.replace(SshSignature.parse(ARMORED), signature_algorithm="rsa-sha2-512")
        with pytest.raises(SignatureFormatError, match="cannot have produced"):
            verify(signature, MESSAGE)

    def test_an_ed25519_scalar_equal_to_the_group_order_is_rejected(self) -> None:
        signature = SshSignature.parse(ARMORED)
        forged = dataclasses.replace(
            signature,
            signature=signature.signature[:32] + ED25519_ORDER.to_bytes(32, "little"),
        )
        with pytest.raises(SignatureInvalidError):
            verify(forged, MESSAGE)

    def test_a_noncanonical_signature_point_is_rejected(self) -> None:
        signature = SshSignature.parse(ARMORED)
        noncanonical_r = ED25519_FIELD.to_bytes(32, "little")
        forged = dataclasses.replace(signature, signature=noncanonical_r + signature.signature[32:])
        with pytest.raises(SignatureInvalidError):
            verify(forged, MESSAGE)

    def test_the_strict_equation_rejects_a_signature_valid_only_after_multiplying_by_the_cofactor(self) -> None:
        # This canonical mixed-order key is the published test key plus the order-two point.
        # The signature satisfies [8][S]B = [8](R + [h]A), but not the required cofactorless
        # [S]B = R + [h]A equation. It pins the backend semantic, not just input encoding.
        mixed_order_key = bytes.fromhex("ea5ef8400c31ef41e28f22e718b43f66981b29cf645af2a0e223799bedaace47")
        cofactored_only_signature = bytes.fromhex(
            "b4b937fca95b2f1e93e41e62fc3c78818ff38a66096fad6e7973e5c90006d321"
            "2aef4d4bc26c21335150064f7cb08df8dcfd375d6b4047a08ae776520b808404"
        )
        blob = put_string(b"ssh-ed25519") + put_string(mixed_order_key)
        forged = dataclasses.replace(
            SshSignature.parse(ARMORED),
            public_key=SshPublicKey.from_blob(blob),
            signature=cofactored_only_signature,
        )
        with pytest.raises(SignatureInvalidError):
            verify(forged, MESSAGE)

    @pytest.mark.parametrize(
        "key_data",
        [
            ED25519_FIELD.to_bytes(32, "little"),
            b"\x01" + (b"\x00" * 31),
            b"\x01" + (b"\x00" * 30) + b"\x80",
        ],
        ids=["noncanonical-y", "small-order", "negative-zero"],
    )
    def test_a_malformed_or_small_order_ed25519_key_is_a_format_error(self, key_data: bytes) -> None:
        blob = put_string(b"ssh-ed25519") + put_string(key_data)
        signature = dataclasses.replace(
            SshSignature.parse(ARMORED),
            public_key=SshPublicKey.from_blob(blob),
        )
        with pytest.raises(SignatureFormatError, match="Ed25519 public key"):
            verify(signature, MESSAGE)

    def test_a_non_empty_outer_reserved_is_accepted_as_openssh_accepts_it(self, signer: SeededSigner) -> None:
        # Trap 2, half one: the outer blob's reserved field is read and discarded.
        signature = dataclasses.replace(sign(MESSAGE, signer), reserved=b"x")
        assert verify(SshSignature.from_blob(signature.to_blob()), MESSAGE).fingerprint == FINGERPRINT

    def test_a_signature_made_over_a_non_empty_reserved_is_rejected_as_openssh_rejects_it(
        self, signer: SeededSigner
    ) -> None:
        # Trap 2, half two: the signed data is reconstructed with an empty reserved field
        # unconditionally, so a signature made over anything else can never verify.
        from boltzmann.authenticity import parse_rfc4253_signature
        from boltzmann.authenticity.sshsig import MAGIC_PREAMBLE, message_hash

        poisoned_signed_data = (
            MAGIC_PREAMBLE
            + put_string(SNAPSHOT_NAMESPACE.encode())
            + put_string(b"x")
            + put_string(b"sha512")
            + put_string(message_hash("sha512", MESSAGE))
        )
        algorithm, raw_signature = parse_rfc4253_signature(signer.sign_blob(poisoned_signed_data))
        forged = dataclasses.replace(
            SshSignature.parse(ARMORED),
            reserved=b"x",
            signature=raw_signature,
            signature_algorithm=algorithm,
        )
        with pytest.raises(SignatureInvalidError):
            verify(forged, MESSAGE)


class TestSigning:
    """Signing frames once, defaults to what OpenSSH emits, and is deterministic."""

    def test_a_signature_signs_verifies_and_round_trips(self, signer: SeededSigner) -> None:
        signature = sign(MESSAGE, signer)
        assert verify(signature, MESSAGE).matches(signer.public_key)
        assert SshSignature.parse(signature.armored()) == signature

    def test_the_default_hash_algorithm_matches_openssh(self, signer: SeededSigner) -> None:
        assert sign(MESSAGE, signer).hash_algorithm == "sha512"

    def test_sha256_is_not_emitted(self, signer: SeededSigner) -> None:
        with pytest.raises(SignatureFormatError, match="sha256"):
            sign(MESSAGE, signer, hash_algorithm="sha256")

    def test_ed25519_signing_is_deterministic_so_an_honest_resign_is_byte_identical(self, signer: SeededSigner) -> None:
        assert sign(MESSAGE, signer).armored() == sign(MESSAGE, signer).armored()

    def test_the_fixture_armor_is_exactly_what_this_sdk_produces(self, signer: SeededSigner) -> None:
        assert sign(MESSAGE, signer).armored() == ARMORED

    def test_normalized_empties_the_reserved_field_and_nothing_else(self, signer: SeededSigner) -> None:
        signature = sign(MESSAGE, signer)
        dirty = dataclasses.replace(signature, reserved=b"x")
        assert normalized(dirty) == signature
        assert normalized(signature) is signature
