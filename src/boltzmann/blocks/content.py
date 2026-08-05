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

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.identity.digest import OciDigest


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
