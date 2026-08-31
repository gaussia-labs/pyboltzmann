"""SSHSIG: the detached signature format snapshots are signed with (paper Section 8.3).

The same mechanism Git uses under ``gpg.format=ssh``. No PKI and no certificate authority:
contributors already hold SSH keys and the forges already publish them. Two blobs matter, and
they are deliberately not the same shape:

The **outer blob** is what the armor contains::

    byte[6]  "SSHSIG"        uint32 1        string publickey
    string   namespace       string reserved string hash_algorithm   string signature

The **signed data** is what the private key actually signs::

    byte[6]  "SSHSIG"   string namespace   string reserved   string hash_algorithm   string H(message)

Three traps, verified against OpenSSH 10.2 rather than recalled:

1. **The signed data carries no version field.** This is the single most common porting bug.
2. **``reserved`` is asymmetric.** OpenSSH ignores a non-empty ``reserved`` in the outer blob
   but reconstructs the signed data with an empty one *unconditionally*, so a signature made
   over a non-empty ``reserved`` is rejected. Always emit empty in both; on verify, always
   reconstruct with empty regardless of what the outer blob carries.
3. **The armor wraps at 70 columns, not 76.** ``PROTOCOL.sshsig`` says a SHOULD of 76 and no
   implementation does it; ``sshbuf_dtob64`` breaks at 70. Emitting 70 makes a record this SDK
   writes byte-identical to one ``ssh-keygen`` wrote. Nothing may depend on the wrap on input.

The message is hashed *before* signing (``H(message)`` above), but Ed25519 here is pure RFC 8032
Ed25519, not Ed25519ph: the whole signed-data blob is what the key signs, and the primitive does
its own internal hashing on top.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace

from boltzmann.authenticity.backend import verify_ed25519
from boltzmann.authenticity.keys import (
    ED25519_KEY_TYPE,
    SshPublicKey,
    parse_rfc4253_signature,
    rfc4253_signature,
)
from boltzmann.authenticity.signers import Signer
from boltzmann.authenticity.wire import WireReader, put_string, put_uint32
from boltzmann.constants import SNAPSHOT_NAMESPACE
from boltzmann.exceptions import (
    NamespaceMismatchError,
    SignatureFormatError,
    SignatureInvalidError,
    UnsupportedKeyTypeError,
)

MAGIC_PREAMBLE = b"SSHSIG"
"""Six raw bytes, no length prefix and no terminator, opening both blobs."""

SIG_VERSION = 1
"""The only SSHSIG version defined. A verifier MUST reject a greater one."""

ARMOR_BEGIN = "-----BEGIN SSH SIGNATURE-----"
ARMOR_END = "-----END SSH SIGNATURE-----"

ARMOR_WRAP = 70
"""Columns the armored base64 wraps at. See trap 3 in the module docstring."""

HASH_ALGORITHMS: dict[str, Callable[[bytes], bytes]] = {
    "sha256": lambda data: hashlib.sha256(data).digest(),
    "sha512": lambda data: hashlib.sha512(data).digest(),
}
"""The two message-hash algorithms the generic SSHSIG framing defines.

An explicit dict rather than ``hashlib.new(name)`` because the name comes out of a signature an
attacker may have written, and ``hashlib.new`` would happily accept ``md5`` or a ``shake_*``
variant whose ``digest()`` wants a length argument and raises deep inside the verifier.
"""

BOLTZMANN_HASH_ALGORITHMS = frozenset({"sha512"})
"""The subset a Boltzmann signature may use. Generic SSHSIG permits SHA-256; this protocol does not."""

DEFAULT_HASH_ALGORITHM = "sha512"
"""What this SDK emits, matching what ``ssh-keygen -Y sign`` emits."""

MAX_ARMORED_LENGTH = 8192
"""Longest armored signature accepted. An Ed25519 one is 318 bytes; RSA-4096 is about 1 KiB."""

MAX_BLOB_LENGTH = 4096
"""Longest decoded signature blob accepted."""


@dataclass(frozen=True, slots=True)
class SshSignature:
    """
    A parsed SSHSIG blob. Structure only -- nothing here asserts the signature is valid.

    Attributes:
        version (int): The SSHSIG version. Always :data:`SIG_VERSION` in this SDK.
        public_key (SshPublicKey): The key embedded in the signature. **This is the authority**:
            a verifier checks this key against the trust root and never a fingerprint named
            elsewhere (paper Section 8.3).
        namespace (str): What the signature was made for.
        reserved (bytes): The outer blob's reserved field, kept for round-trip fidelity. Ignored
            on verify (see trap 2 in the module docstring); always empty on anything this SDK signs.
        hash_algorithm (str): How the message was hashed into the signed data.
        signature_algorithm (str): The algorithm named inside the signature blob. Equal to the
            key type for Ed25519; legitimately different for RSA (``rsa-sha2-512``).
        signature (bytes): The raw signature bytes -- 64 for Ed25519.
    """

    version: int
    public_key: SshPublicKey
    namespace: str
    reserved: bytes
    hash_algorithm: str
    signature_algorithm: str
    signature: bytes

    # --- Wire form --------------------------------------------------------------

    def to_blob(self) -> bytes:
        """
        Encode the outer SSHSIG blob.

        Returns:
            bytes: The wire-encoded signature.
        """
        return (
            MAGIC_PREAMBLE
            + put_uint32(self.version)
            + put_string(self.public_key.blob)
            + put_string(self.namespace.encode("utf-8"))
            + put_string(self.reserved)
            + put_string(self.hash_algorithm.encode("ascii"))
            + put_string(rfc4253_signature(self.signature_algorithm, self.signature))
        )

    @classmethod
    def from_blob(cls, blob: bytes) -> SshSignature:
        """
        Decode an outer SSHSIG blob.

        Args:
            blob (bytes): The wire-encoded signature.

        Returns:
            SshSignature: The parsed structure.

        Raises:
            SignatureFormatError: On a bad preamble, an unsupported version, an empty namespace,
                a hash algorithm outside :data:`BOLTZMANN_HASH_ALGORITHMS`, a malformed key or signature
                blob, or trailing bytes.
        """
        if len(blob) > MAX_BLOB_LENGTH:
            raise SignatureFormatError(f"SSHSIG blob of {len(blob)} bytes exceeds the {MAX_BLOB_LENGTH} cap")
        reader = WireReader(blob)
        magic = reader.fixed(len(MAGIC_PREAMBLE))
        if magic != MAGIC_PREAMBLE:
            raise SignatureFormatError(f"not an SSHSIG blob: preamble is {magic!r}")
        version = reader.uint32()
        if version != SIG_VERSION:
            raise SignatureFormatError(f"SSHSIG version {version} is not implemented here; this SDK reads version 1")
        public_key = SshPublicKey.from_blob(reader.string())
        raw_namespace = reader.string()
        if not raw_namespace:
            raise SignatureFormatError("an SSHSIG namespace must not be empty")
        try:
            namespace = raw_namespace.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SignatureFormatError(f"SSHSIG namespace {raw_namespace!r} is not UTF-8") from error
        reserved = reader.string()
        raw_hash = reader.string()
        try:
            hash_algorithm = raw_hash.decode("ascii")
        except UnicodeDecodeError as error:
            raise SignatureFormatError(f"SSHSIG hash algorithm {raw_hash!r} is not ASCII") from error
        if hash_algorithm not in BOLTZMANN_HASH_ALGORITHMS:
            allowed = ", ".join(sorted(BOLTZMANN_HASH_ALGORITHMS))
            raise SignatureFormatError(f"SSHSIG hash algorithm {hash_algorithm!r} is not allowed; expected {allowed}")
        signature_algorithm, signature = parse_rfc4253_signature(reader.string())
        reader.finish()
        return cls(
            version=version,
            public_key=public_key,
            namespace=namespace,
            reserved=reserved,
            hash_algorithm=hash_algorithm,
            signature_algorithm=signature_algorithm,
            signature=signature,
        )

    # --- Armor ------------------------------------------------------------------

    def armored(self) -> str:
        """
        The armored text form, byte-identical to what ``ssh-keygen -Y sign`` writes.

        Returns:
            str: The armored signature, ending in a newline.
        """
        return armor(self.to_blob())

    @classmethod
    def parse(cls, armored_text: str) -> SshSignature:
        """
        Dearmor and decode a signature.

        Args:
            armored_text (str): The ``-----BEGIN SSH SIGNATURE-----`` container.

        Returns:
            SshSignature: The parsed structure.

        Raises:
            SignatureFormatError: If the armor or the blob inside it cannot be read.
        """
        return cls.from_blob(dearmor(armored_text))

    @property
    def fingerprint(self) -> str:
        """The embedded key's fingerprint, for reporting."""
        return self.public_key.fingerprint


def armor(blob: bytes, wrap: int = ARMOR_WRAP) -> str:
    """
    Wrap a blob in the SSH signature armor.

    Args:
        blob (bytes): The bytes to armor.
        wrap (int): Columns to wrap the base64 at. Defaults to :data:`ARMOR_WRAP`; see trap 3.

    Returns:
        str: The armored container, ending in a newline.
    """
    encoded = base64.b64encode(blob).decode("ascii")
    lines = [encoded[start : start + wrap] for start in range(0, len(encoded), wrap)] or [""]
    return f"{ARMOR_BEGIN}\n" + "\n".join(lines) + f"\n{ARMOR_END}\n"


def dearmor(armored_text: str) -> bytes:
    """
    Extract the blob from an armored container.

    Accepts any base64 wrapping and CRLF line endings, as OpenSSH's parser does, and requires
    the BEGIN marker at offset zero, as ``sshsig_dearmor`` does.

    Args:
        armored_text (str): The armored container.

    Returns:
        bytes: The decoded blob.

    Raises:
        SignatureFormatError: If the markers are absent or misplaced, the input exceeds the cap,
            or the base64 does not decode.
    """
    if len(armored_text) > MAX_ARMORED_LENGTH:
        raise SignatureFormatError(
            f"armored signature of {len(armored_text)} characters exceeds the {MAX_ARMORED_LENGTH} cap"
        )
    if not armored_text.startswith(ARMOR_BEGIN):
        raise SignatureFormatError(f"an armored signature starts with {ARMOR_BEGIN!r} at offset zero")
    remainder = armored_text[len(ARMOR_BEGIN) :]
    body, separator, _trailer = remainder.partition(ARMOR_END)
    if not separator:
        raise SignatureFormatError(f"armored signature is missing its {ARMOR_END!r} footer")
    condensed = "".join(body.split())
    try:
        return base64.b64decode(condensed, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SignatureFormatError(f"armored signature body does not decode as base64: {error}") from error


def message_hash(hash_algorithm: str, message: bytes) -> bytes:
    """
    Hash the message under a named, allowed algorithm.

    Args:
        hash_algorithm (str): ``"sha256"`` or ``"sha512"`` from generic SSHSIG framing.
        message (bytes): The bytes being signed -- for a snapshot, its canonical bytes.

    Returns:
        bytes: The raw digest.

    Raises:
        SignatureFormatError: If the algorithm is not defined by SSHSIG.
    """
    digest = HASH_ALGORITHMS.get(hash_algorithm)
    if digest is None:
        allowed = ", ".join(sorted(HASH_ALGORITHMS))
        raise SignatureFormatError(f"hash algorithm {hash_algorithm!r} is not allowed; expected {allowed}")
    return digest(message)


def signed_data(message: bytes, namespace: str, hash_algorithm: str = DEFAULT_HASH_ALGORITHM) -> bytes:
    """
    Build the bytes the private key actually signs.

    Note what is absent relative to the outer blob: no version field and no public key (trap 1
    in the module docstring). ``reserved`` is always written empty, because OpenSSH reconstructs
    it empty unconditionally when verifying (trap 2), so a signature made over anything else
    cannot be verified by ``ssh-keygen``.

    Args:
        message (bytes): The bytes being signed.
        namespace (str): The namespace the signature is bound to. Must not be empty.
        hash_algorithm (str): How to hash the message. Defaults to sha512, OpenSSH's default.

    Returns:
        bytes: The signed-data blob.

    Raises:
        SignatureFormatError: If the namespace is empty or the algorithm is not allowed.
    """
    if not namespace:
        raise SignatureFormatError("an SSHSIG namespace must not be empty")
    return (
        MAGIC_PREAMBLE
        + put_string(namespace.encode("utf-8"))
        + put_string(b"")
        + put_string(hash_algorithm.encode("ascii"))
        + put_string(message_hash(hash_algorithm, message))
    )


def verify(signature: SshSignature, message: bytes, namespace: str = SNAPSHOT_NAMESPACE) -> SshPublicKey:
    """
    Check a signature over ``message`` and return the key that made it.

    ``namespace`` is what stops a signature a contributor produced for a Git commit being
    presented as a Boltzmann one, and a verifier that took it from the signature would be
    checking the attacker's claim against itself -- so it is a parameter of the caller's, with
    the protocol's as its default.

    Whether the returned key is *authorized* is a separate question, answered against the trust
    root in force at the snapshot's position; this function answers only "did this key produce
    this signature over these bytes".

    Args:
        signature (SshSignature): The parsed signature.
        message (bytes): The exact bytes the signature claims to cover.
        namespace (str): The namespace the caller requires.

    Returns:
        SshPublicKey: The embedded key, which is the identity every later decision uses.

    Raises:
        NamespaceMismatchError: If the signature was made under another namespace.
        UnsupportedKeyTypeError: If the embedded key is not one this SDK verifies.
        WeakKeyError: If the embedded key is below the protocol security floor.
        SignatureFormatError: If the signature's algorithm disagrees with its key type.
        SignatureInvalidError: If the mathematics failed.
        VerificationUnavailableError: If the ``[authenticity]`` extra is not installed.
    """
    if signature.namespace != namespace:
        raise NamespaceMismatchError(
            f"signature was made under namespace {signature.namespace!r}, not {namespace!r}; it is "
            f"genuine and it covers something else"
        )
    key = signature.public_key
    if signature.hash_algorithm not in BOLTZMANN_HASH_ALGORITHMS:
        allowed = ", ".join(sorted(BOLTZMANN_HASH_ALGORITHMS))
        raise SignatureFormatError(
            f"SSHSIG hash algorithm {signature.hash_algorithm!r} is not allowed; expected {allowed}"
        )
    key.require_security_floor()
    if not key.is_supported:
        raise UnsupportedKeyTypeError(
            f"key type {key.key_type!r} ({key.fingerprint}) is not one this SDK verifies; the "
            f"signature may be perfectly valid and this client too narrow to check it"
        )
    if signature.signature_algorithm != ED25519_KEY_TYPE:
        raise SignatureFormatError(f"an Ed25519 key cannot have produced a {signature.signature_algorithm!r} signature")
    data = signed_data(message, namespace=signature.namespace, hash_algorithm=signature.hash_algorithm)
    if not verify_ed25519(key.key_data, signature.signature, data):
        raise SignatureInvalidError(
            f"signature by {key.fingerprint} does not verify over the {len(message)}-byte message "
            f"under namespace {namespace!r}"
        )
    return key


def sign(
    message: bytes,
    signer: Signer,
    namespace: str = SNAPSHOT_NAMESPACE,
    hash_algorithm: str = DEFAULT_HASH_ALGORITHM,
) -> SshSignature:
    """
    Produce a detached SSHSIG signature over ``message``.

    The framing happens here, exactly once: the signer receives the signed-data blob and returns
    an RFC 4253 signature blob, which is dropped into the outer structure verbatim. Because the
    Ed25519 mathematics runs inside the signer's backend -- an ssh-agent, ordinarily -- signing
    works without the ``[authenticity]`` extra installed.

    Args:
        message (bytes): The bytes to sign -- for a snapshot, its canonical bytes.
        signer (Signer): What produces the raw signature.
        namespace (str): The namespace to bind the signature to.
        hash_algorithm (str): How to hash the message. Defaults to sha512.

    Returns:
        SshSignature: The complete signature, ready to armor.

    Raises:
        SignatureFormatError: If the namespace is empty, the algorithm is not allowed, or the
            signer returned a signature whose algorithm disagrees with its key.
        SignerUnavailableError: If the signing backend cannot sign.
    """
    if hash_algorithm not in BOLTZMANN_HASH_ALGORITHMS:
        allowed = ", ".join(sorted(BOLTZMANN_HASH_ALGORITHMS))
        raise SignatureFormatError(f"hash algorithm {hash_algorithm!r} is not allowed; expected {allowed}")
    data = signed_data(message, namespace=namespace, hash_algorithm=hash_algorithm)
    algorithm, raw = parse_rfc4253_signature(signer.sign_blob(data))
    key = signer.public_key
    if key.is_ed25519 and algorithm != ED25519_KEY_TYPE:
        raise SignatureFormatError(
            f"signer holds an Ed25519 key ({key.fingerprint}) but returned a {algorithm!r} signature"
        )
    return SshSignature(
        version=SIG_VERSION,
        public_key=key,
        namespace=namespace,
        reserved=b"",
        hash_algorithm=hash_algorithm,
        signature_algorithm=algorithm,
        signature=raw,
    )


def normalized(signature: SshSignature) -> SshSignature:
    """
    The signature with its ``reserved`` field emptied, as this SDK would have written it.

    Armor wrap and the reserved field are producer choices, so record identity must never be
    derived from armored text; comparing or storing through this puts two producers of the same
    signature on the same bytes.

    Args:
        signature (SshSignature): The signature to normalize.

    Returns:
        SshSignature: The same signature over the same bytes, in canonical form.
    """
    if not signature.reserved:
        return signature
    return replace(signature, reserved=b"")
