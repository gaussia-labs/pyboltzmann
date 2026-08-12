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

from email import policy

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


MAX_MEDIA_TYPE_PART = 127
"""Longest a type or subtype may be, from RFC 6838 Section 4.2."""


def require_media_type(value: str, *, what: str = "content") -> str:
    """
    Refuse anything that is not a bare, canonically spelled ``type/subtype``.

    Parsing is :mod:`email.headerregistry`'s, which is the RFC 2045 grammar as the standard
    library implements it -- so what counts as a media type here is not this module's opinion,
    and header injection, unbalanced quoting and the rest are somebody else's solved problem.
    Its parser reports defects rather than raising, so a defect is the refusal.

    On top of parsing, the value has to be **exactly** what the parse reconstructs. That single
    comparison is what rejects the things a parser is right to tolerate but an identity cannot
    afford, because ``media_type`` is hashed into ``block_id``:

    * ``'image/png; charset=utf-8'`` -- parameters describe *this* transfer, not the bytes, so
      the same content would land under two identities depending on who wrote it down.
    * ``'IMAGE/PNG'`` -- media types are case-insensitive to compare and case-sensitive to hash,
      which is the same split by another route. Refused rather than lowercased, for the reason
      ``LocalLayoutRegistry`` refuses a reference rather than sanitising it: rewriting what a
      caller passed would file their content under something nobody asked for.
    * ``'image/png;'``, ``'image/png '`` -- spellings a parser forgives and a digest does not.

    Deliberately not a registry lookup. IANA moves without this SDK moving, and a brain is not
    where someone should discover that a vendor tree was published last Tuesday. ``'png'`` and
    ``' '`` are wrong under every registry, and they are the mistakes that actually happen.

    **Checked where bytes are written, never where a block is decoded.** Validating in a model
    validator would run on every ``decode``, so a brain already published with a malformed media
    type would stop being readable -- by a client trying to be more correct than the one that
    wrote it. Refusing at the boundary keeps the bad value out, which is the only place refusing
    it costs nothing.

    Args:
        value (str): The media type to check.
        what (str): What is being described, for the error message.

    Returns:
        str: ``value`` unchanged.

    Raises:
        ProtocolError: If it is not a bare, lowercase ``type/subtype``.
    """
    try:
        parsed = policy.default.header_factory("content-type", value)
        canonical = f"{parsed.maintype}/{parsed.subtype}"
        defects = list(parsed.defects)
    except Exception:
        # The parser reports defects rather than raising, so raising at all means the value is
        # malformed in a way it had no defect for. Broad because this is untrusted input and the
        # only documented failure of this function is ProtocolError.
        defects, canonical = ["unparseable"], ""

    if defects or canonical != value:
        raise ProtocolError(
            f"{value!r} is not a usable media type: {what} must declare a bare, lowercase "
            f"'type/subtype' such as 'image/png' or 'application/octet-stream' -- no parameters, no "
            f"trailing punctuation. It is recorded in the payload and hashed into the block's "
            f"identity, so it cannot be corrected later without writing a different block"
        )

    if any(len(part) > MAX_MEDIA_TYPE_PART for part in (parsed.maintype, parsed.subtype)):
        raise ProtocolError(
            f"{value!r} is not a usable media type: RFC 6838 bounds a type and a subtype at "
            f"{MAX_MEDIA_TYPE_PART} characters each"
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
