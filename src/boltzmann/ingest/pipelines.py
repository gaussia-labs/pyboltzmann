"""Named deterministic pipelines for normalized views.

A normalized view must be reproducible: given the same original and the same pipeline at
the same version, any client must obtain the same bytes, or the view's content address
would differ between clients and stop being evidence. That is why a pipeline is
registered under a name *and* a version, and why both are recorded in provenance.

Nothing model-based belongs here. An extraction that calls a language model is not
deterministic, so its output is a proposal for semantic memory, not a normalized view of
canonical evidence.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from boltzmann.exceptions import ProtocolError


@runtime_checkable
class NormalizationPipeline(Protocol):
    """Turns observed bytes into a normalized view, deterministically."""

    @property
    def name(self) -> str:
        """Registered name of the pipeline, recorded in provenance."""
        ...

    @property
    def version(self) -> str:
        """Version of the pipeline, recorded in provenance."""
        ...

    @property
    def output_media_type(self) -> str:
        """Media type of the normalized bytes this pipeline produces."""
        ...

    def accepts(self, media_type: str) -> bool:
        """
        Whether this pipeline can normalize a given input.

        Args:
            media_type (str): Media type of the original.

        Returns:
            bool: Whether the pipeline applies.
        """
        ...

    def normalize(self, data: bytes) -> bytes:
        """
        Produce the normalized view.

        Args:
            data (bytes): The original bytes.

        Returns:
            bytes: The normalized bytes. Must be a pure function of ``data``.
        """
        ...


_PIPELINES: dict[str, NormalizationPipeline] = {}


def register_pipeline(pipeline: NormalizationPipeline) -> None:
    """
    Make a pipeline available by name.

    Args:
        pipeline (NormalizationPipeline): The pipeline to register.

    Raises:
        ProtocolError: If the name is already taken by a different pipeline.
    """
    existing = _PIPELINES.get(pipeline.name)
    if existing is not None and existing is not pipeline:
        raise ProtocolError(
            f"a different pipeline is already registered as {pipeline.name!r}; a name and version "
            f"must identify exactly one transform for a normalized view to be reproducible"
        )
    _PIPELINES[pipeline.name] = pipeline


def get_pipeline(name: str) -> NormalizationPipeline:
    """
    Resolve a pipeline by name.

    Args:
        name (str): Registered name.

    Returns:
        NormalizationPipeline: The pipeline.

    Raises:
        ProtocolError: If no pipeline is registered under that name.
    """
    try:
        return _PIPELINES[name]
    except KeyError:
        known = ", ".join(sorted(_PIPELINES)) or "none"
        raise ProtocolError(f"no normalization pipeline registered as {name!r}; registered: {known}") from None


def available_pipelines() -> dict[str, NormalizationPipeline]:
    """
    Every registered pipeline.

    Returns:
        dict[str, NormalizationPipeline]: Pipelines keyed by name.
    """
    return dict(_PIPELINES)
