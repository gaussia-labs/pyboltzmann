"""Content a block names instead of inlining (paper Section 6.1).

A payload is JSON, canonically serialized and hashed on every access, so a value
large enough to matter does not belong inside one. A block may therefore *name*
its content by digest and keep the bytes in the store, exactly as a canonical
block names the original it describes.

This is a decision about **representation, not meaning**. A block whose content
sits out of line asserts the same thing as one that inlines it; the digest binds
the bytes into ``block_id`` just as firmly as the text would have, because a
content address is a function of the content.

**Content is not evidence.** Evidence is canonical: it lives in the canonical
composition, other blocks cite it, and dropping it cascades to everything derived
from it. Content is the block's own datum -- the transcript of *that* episode --
so nothing cites it and nothing needs to. It lives and dies with its block: once
the block leaves the composition nothing reaches the bytes and ``prune`` reclaims
them. A source that other blocks will cite is a canonical block, through
``register``, and always was.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.exceptions import ProtocolError
from boltzmann.identity.digest import Digest, OciDigest


class ContentRef(BaseModel):
    """
    Bytes a block names rather than carries.

    The three fields are an OCI descriptor over the content: what it is addressed
    by, what it is, and how much of it there is. ``size`` and ``media_type`` are
    part of the block rather than looked up from the store so that a consumer can
    tell what a block names -- and decide whether to fetch it -- without holding
    the bytes at all.

    Attributes:
        blob (OciDigest): Content address of the bytes.
        media_type (str): IANA media type of the bytes.
        size (int): Length of the bytes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    blob: OciDigest
    media_type: str = Field(min_length=1)
    size: int = Field(ge=0)


MEDIA_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$")
"""The shape RFC 6838 gives a media type: ``type/subtype``, restricted to the characters it allows.

Deliberately a shape check and not a registry lookup. The IANA registry changes without this SDK
changing, and a brain is not the place to learn that a vendor tree was published last Tuesday --
but ``" "`` and ``"png"`` are wrong under any registry, and they are the mistakes that actually
happen.
"""


def require_media_type(value: str, *, what: str = "content") -> str:
    """
    Refuse a media type that is not ``type/subtype``.

    **Checked where bytes are written, never where a block is decoded.** A media type reaches a block
    payload and is therefore hashed into ``block_id``, so validating it in a model validator would run
    on every ``decode`` -- and any brain already published with a malformed one would stop being
    readable, by a client that is trying to be more correct than the one that wrote it. Refusing at the
    boundary keeps the bad value from entering, which is the only place refusing it costs nothing.

    Args:
        value (str): The media type to check.
        what (str): What is being described, for the error message.

    Returns:
        str: ``value`` unchanged.

    Raises:
        ProtocolError: If it is not of the form ``type/subtype``.
    """
    if not MEDIA_TYPE_PATTERN.match(value):
        raise ProtocolError(
            f"{value!r} is not a media type: {what} must declare one as 'type/subtype', such as "
            f"'image/png' or 'application/octet-stream'. It is recorded in the payload and hashed into "
            f"the block's identity, so it cannot be corrected later without writing a different block"
        )
    return value


class NamesContent(BaseModel):
    """
    Mixin for a block schema whose datum may sit in the store rather than in its payload.

    Carries the field and the :attr:`~boltzmann.blocks.base.Block.content_digests` override
    together, because a schema that declared one without the other would be the specific bug
    this mixin exists to make unrepresentable: content nothing reports is content a prune
    reclaims while the block still names it, and a redaction leaves behind.

    Not a :class:`~boltzmann.blocks.base.Block` itself, so it registers no schema of its own --
    it is mixed into a concrete version alongside the block class it extends.

    Attributes:
        content (ContentRef | None): The block's own datum, when it is too large or too binary
            to live in a JSON payload. Optional, and absent by default: a block that inlines
            everything it states is the ordinary case, and an absent optional field is dropped
            from the payload, so adding this mixin to a new schema version cannot change what a
            self-contained block hashes to beyond the version in its envelope.
    """

    content: ContentRef | None = None

    @property
    def content_digests(self) -> tuple[Digest, ...]:
        """The content this block names, empty when it carries its datum inline."""
        return () if self.content is None else (self.content.blob,)
