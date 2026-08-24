"""SSH public keys: the identity a signature embeds and a trust root lists.

A key travels in two forms and both appear in the protocol. The *blob* is the RFC 4253 binary
encoding (``string`` key type followed by type-specific fields), which is what an SSHSIG
signature embeds and what a fingerprint hashes. The *authorized_keys form* is the one-line text
encoding ``<type> <base64-of-blob>``, which is how a trust root lists a key (paper Section 8.5).

This module holds the blob, not a ``cryptography`` object, so that a reader without the
``[authenticity]`` extra can still parse a trust root, compute fingerprints, and reject a record
whose fingerprint disagrees with the key inside its signature. Only turning a key into a
verifier needs the extra.

**A trust-root entry has exactly two fields.** An ``authorized_keys`` line in the wild may carry
an options prefix and a trailing comment; a trust root entry may not, because two spellings of
one key would be two different trust-root digests, and a pin has to mean something. The
canonical form is ``"<type> <base64>"``, byte-exact, and anything else is rejected.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

from boltzmann.authenticity.wire import WireReader, put_string
from boltzmann.exceptions import SignatureFormatError

ED25519_KEY_TYPE = "ssh-ed25519"
"""The RECOMMENDED key type (paper Section 8.3), and the only one this SDK verifies."""

ED25519_KEY_BYTES = 32
"""Length of a raw Ed25519 public key."""

SUPPORTED_KEY_TYPES = frozenset({ED25519_KEY_TYPE})
"""Key types this version of the SDK can verify signatures from.

Other types still *parse* -- fingerprints, "which key is this", and error messages stay precise
-- but verification reports them as unsupported rather than invalid: a well-formed signature
this client cannot check is not a forgery.
"""

FINGERPRINT_PATTERN = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
"""Shape of a SHA-256 SSH fingerprint: 32 hashed bytes are 44 base64 characters, one ``=`` stripped."""

MAX_KEY_BLOB = 1 << 14
"""Longest key blob accepted. An Ed25519 blob is 51 bytes; RSA-4096 is under 1 KiB."""

_AUTHORIZED_KEY_PATTERN = re.compile(r"^(\S+) ([A-Za-z0-9+/=]+)$")
"""Exactly two fields, one space, standard base64. No options prefix, no comment."""


@dataclass(frozen=True, slots=True)
class SshPublicKey:
    """
    An SSH public key as it travels: a type name and the RFC 4253 blob.

    The blob is the source of truth -- it is what gets fingerprinted, what a signature embeds,
    and what two keys are compared on. The type name is carried redundantly (it is also the
    blob's first field) so that mismatched encodings are a construction error rather than a
    latent one.

    Attributes:
        key_type (str): The SSH key type name, such as ``ssh-ed25519``.
        blob (bytes): The full RFC 4253 public key blob, whose first field names ``key_type``.
    """

    key_type: str
    blob: bytes

    KIND: ClassVar[str] = "ssh public key"
    """Human-readable name of this identity, used in error messages."""

    def __post_init__(self) -> None:
        if not self.key_type:
            raise SignatureFormatError("an SSH public key names its type; got an empty one")
        if len(self.blob) > MAX_KEY_BLOB:
            raise SignatureFormatError(f"SSH public key blob of {len(self.blob)} bytes exceeds the {MAX_KEY_BLOB} cap")
        reader = WireReader(self.blob)
        declared = reader.string()
        if declared != self.key_type.encode("ascii", errors="replace"):
            raise SignatureFormatError(
                f"SSH public key blob declares type {declared!r} but was constructed as {self.key_type!r}"
            )
        # Every SSH public key blob is a sequence of strings (mpints are string-framed), so the
        # whole structure can be walked without knowing the type. A blob that does not frame
        # cleanly would fingerprint fine and then fail everywhere else; reject it here instead.
        while reader.remaining:
            reader.string()
        if self.key_type == ED25519_KEY_TYPE:
            key_data = WireReader(self.blob)
            key_data.string()
            raw = key_data.string()
            if len(raw) != ED25519_KEY_BYTES:
                raise SignatureFormatError(
                    f"an Ed25519 public key is {ED25519_KEY_BYTES} bytes; this blob carries {len(raw)}"
                )

    # --- Construction ---------------------------------------------------------

    @classmethod
    def from_blob(cls, blob: bytes) -> SshPublicKey:
        """
        Parse an RFC 4253 public key blob, as a signature or an ssh-agent embeds it.

        Args:
            blob (bytes): The wire-encoded public key.

        Returns:
            SshPublicKey: The parsed key.

        Raises:
            SignatureFormatError: If the blob does not frame as an SSH public key.
        """
        reader = WireReader(blob)
        declared = reader.string()
        try:
            key_type = declared.decode("ascii")
        except UnicodeDecodeError as error:
            raise SignatureFormatError(f"SSH key type {declared!r} is not ASCII") from error
        return cls(key_type=key_type, blob=blob)

    @classmethod
    def parse(cls, value: Any) -> SshPublicKey:
        """
        Parse the authorized_keys form a trust root entry carries.

        Strict on purpose: exactly ``"<type> <base64>"``, no options field, no comment, standard
        base64 with padding. Two spellings of one key would be two different trust-root digests
        (see the module docstring), so the canonical form is the only accepted one.

        Args:
            value (Any): A ``"<type> <base64>"`` string, or an instance of this class.

        Returns:
            SshPublicKey: The parsed key.

        Raises:
            SignatureFormatError: If the line is not in canonical form, the base64 does not
                decode, or the encoded type disagrees with the named one.
        """
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise SignatureFormatError(f"expected an SSH public key string, got {type(value).__name__}")
        match = _AUTHORIZED_KEY_PATTERN.match(value)
        if match is None:
            raise SignatureFormatError(
                "an SSH public key entry is exactly '<type> <base64>'; options, comments, and extra "
                "whitespace are rejected because two spellings of one key would be two identities"
            )
        key_type, encoded = match.groups()
        try:
            blob = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise SignatureFormatError(f"SSH public key base64 does not decode: {error}") from error
        key = cls.from_blob(blob)
        if key.key_type != key_type:
            raise SignatureFormatError(
                f"SSH public key line names type {key_type!r} but its blob encodes {key.key_type!r}"
            )
        return key

    # --- Access ---------------------------------------------------------------

    @property
    def fingerprint(self) -> str:
        """
        The key's SHA-256 fingerprint, as ``ssh-keygen -lf`` prints it.

        ``"SHA256:" + base64(sha256(blob))`` with the padding stripped. The bytes hashed are the
        RFC 4253 blob -- not the raw key bytes and not the text line.
        """
        digest = hashlib.sha256(self.blob).digest()
        return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")

    @property
    def authorized_key(self) -> str:
        """The canonical text form a trust root stores: ``"<type> <base64>"``, nothing else."""
        return f"{self.key_type} {base64.b64encode(self.blob).decode('ascii')}"

    @property
    def is_ed25519(self) -> bool:
        """Whether this is an ``ssh-ed25519`` key."""
        return self.key_type == ED25519_KEY_TYPE

    @property
    def is_supported(self) -> bool:
        """Whether this SDK can verify signatures made by this key."""
        return self.key_type in SUPPORTED_KEY_TYPES

    @property
    def key_data(self) -> bytes:
        """
        The raw key bytes: the blob's second field.

        For Ed25519 this is the 32-byte public point, which is what the verify primitive takes.
        """
        reader = WireReader(self.blob)
        reader.string()
        return reader.string()

    def matches(self, other: SshPublicKey) -> bool:
        """
        Whether two values are the same key.

        Compared on the full blob with a constant-time comparison. Never compare fingerprints
        instead: a verifier must not decide on a 32-byte hash of the thing it is deciding about.

        Args:
            other (SshPublicKey): The key to compare against.

        Returns:
            bool: Whether the blobs are byte-identical.
        """
        return hmac.compare_digest(self.blob, other.blob)

    def __str__(self) -> str:
        return self.authorized_key

    # --- Pydantic integration -------------------------------------------------

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        """Accept an instance or the authorized_keys string form; emit the string form."""
        from_str = core_schema.no_info_plain_validator_function(cls.parse)
        return core_schema.json_or_python_schema(
            json_schema=from_str,
            python_schema=core_schema.union_schema([core_schema.is_instance_schema(cls), from_str]),
            serialization=core_schema.plain_serializer_function_ser_schema(
                str, return_schema=core_schema.str_schema(), when_used="always"
            ),
        )


def rfc4253_signature(algorithm: str, raw: bytes) -> bytes:
    """
    Frame a raw signature as an RFC 4253 signature blob: ``string type || string bytes``.

    This is the form an ssh-agent returns and the form SSHSIG's ``signature`` field carries, so
    the two compose without re-framing.

    Args:
        algorithm (str): The signature algorithm name, such as ``ssh-ed25519``.
        raw (bytes): The raw signature bytes.

    Returns:
        bytes: The framed signature blob.
    """
    return put_string(algorithm.encode("ascii")) + put_string(raw)


def parse_rfc4253_signature(blob: bytes) -> tuple[str, bytes]:
    """
    Split an RFC 4253 signature blob into its algorithm name and raw bytes.

    Args:
        blob (bytes): The framed signature.

    Returns:
        tuple[str, bytes]: The algorithm name and the raw signature bytes.

    Raises:
        SignatureFormatError: If the blob does not frame cleanly or carries trailing bytes.
    """
    reader = WireReader(blob)
    algorithm = reader.string()
    raw = reader.string()
    reader.finish()
    try:
        name = algorithm.decode("ascii")
    except UnicodeDecodeError as error:
        raise SignatureFormatError(f"signature algorithm {algorithm!r} is not ASCII") from error
    return name, raw
