"""SSH wire primitives: the framing everything in this package is built from.

SSH encodes structures out of three primitives (RFC 4251, Section 5): raw bytes, a big-endian
``uint32``, and a ``string`` -- a ``uint32`` byte count followed by exactly that many bytes.
There is no ASN.1 and no DER. SSHSIG blobs, public key blobs, signature blobs, and the
ssh-agent protocol are all built from these three, which is why the codec lives in one place
and both the signature layer and the agent client read through it.

Reading is bounded and single-pass. A length prefix is attacker-controlled input, so it is
checked against what remains of the buffer *before* any slice: a declared length is never
trusted enough to allocate against.
"""

from __future__ import annotations

import struct

from boltzmann.exceptions import SignatureFormatError

MAX_STRING = 1 << 16
"""Longest SSH ``string`` this codec will decode.

An SSHSIG blob for any key type OpenSSH supports is under 2 KiB; the cap is what stops a
four-byte length prefix from being a memory allocation request.
"""

_UINT32 = struct.Struct(">I")


def put_uint32(value: int) -> bytes:
    """
    Encode an SSH ``uint32``: four bytes, big-endian.

    Args:
        value (int): The value to encode. Must fit in 32 bits.

    Returns:
        bytes: The four encoded bytes.

    Raises:
        SignatureFormatError: If ``value`` is negative or does not fit in 32 bits.
    """
    if not 0 <= value < 1 << 32:
        raise SignatureFormatError(f"value {value} does not fit in an SSH uint32")
    return _UINT32.pack(value)


def put_string(value: bytes) -> bytes:
    """
    Encode an SSH ``string``: a ``uint32`` byte count followed by the bytes.

    Args:
        value (bytes): The bytes to frame. Not required to be UTF-8; SSH strings are octets.

    Returns:
        bytes: The framed bytes.
    """
    return put_uint32(len(value)) + value


class WireReader:
    """
    A single-pass bounded reader over an SSH wire blob.

    Every read checks the declared length against what remains *before* slicing, so truncation
    is always a typed error and never short data. ``finish`` is part of the contract, not a
    nicety: a blob with trailing bytes must be rejected, or an attacker can append material a
    lenient parser ignores and a strict one does not -- OpenSSH errors on trailing data in a
    signature blob for the same reason.

    Attributes:
        max_string (int): Longest ``string`` this reader will accept.
    """

    def __init__(self, data: bytes, max_string: int = MAX_STRING) -> None:
        """
        Open a reader over a blob.

        Args:
            data (bytes): The wire bytes to read.
            max_string (int): Longest ``string`` to accept. Defaults to :data:`MAX_STRING`.
        """
        self._data = data
        self._offset = 0
        self.max_string = max_string

    @property
    def remaining(self) -> int:
        """How many bytes have not been read yet."""
        return len(self._data) - self._offset

    def fixed(self, count: int) -> bytes:
        """
        Read exactly ``count`` raw bytes.

        Args:
            count (int): How many bytes to read.

        Returns:
            bytes: The bytes read.

        Raises:
            SignatureFormatError: If fewer than ``count`` bytes remain.
        """
        if count < 0 or self.remaining < count:
            raise SignatureFormatError(
                f"truncated SSH wire data: needed {count} bytes at offset {self._offset}, {self.remaining} remain"
            )
        value = self._data[self._offset : self._offset + count]
        self._offset += count
        return value

    def uint32(self) -> int:
        """
        Read an SSH ``uint32``.

        Returns:
            int: The decoded value.

        Raises:
            SignatureFormatError: If fewer than four bytes remain.
        """
        return int(_UINT32.unpack(self.fixed(4))[0])

    def string(self) -> bytes:
        """
        Read an SSH ``string``.

        The declared length is validated against both the cap and the remaining buffer before
        any slice happens.

        Returns:
            bytes: The string's bytes.

        Raises:
            SignatureFormatError: If the declared length exceeds the cap or the remaining bytes.
        """
        length = self.uint32()
        if length > self.max_string:
            raise SignatureFormatError(
                f"SSH string declares {length} bytes, which exceeds the {self.max_string}-byte cap; "
                f"a length prefix is not a memory allocation request"
            )
        return self.fixed(length)

    def finish(self) -> None:
        """
        Assert that every byte was consumed.

        Raises:
            SignatureFormatError: If any bytes trail the structure that was read.
        """
        if self.remaining:
            raise SignatureFormatError(f"SSH wire data carries {self.remaining} trailing bytes after the structure")
