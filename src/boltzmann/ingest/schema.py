"""JSON Schema for the wire formats, so a model can be constrained to produce them.

A :class:`~boltzmann.ingest.task.ProcessingTask` tells the model that its answer must satisfy
``boltzmann.candidates/v1``. That is a *name*. Without the schema behind it the model has to guess the
shape, and the payload is the part it most needs to get right: ``Candidate.payload`` is typed
``dict[str, Any]`` in Python, so pydantic's own schema for it says "any object" and offers no hint that
a semantic block needs ``kind``, ``label`` and ``statement``, or that ``kind`` is one of five values.

The SDK already knows all of it -- the block classes are the schema. This module composes it: one
candidate variant per memory type, each pinning ``memory_type`` to a constant and replacing the opaque
payload with that type's block schema. The variants are joined by ``oneOf`` so a validator can tell the
model exactly which shape a ``"semantic"`` candidate must have.

Restricting to a task matters too. A task that allows only semantic blocks produces a schema with one
variant, so a model constrained by it cannot propose an episode that the gate would reject anyway.

This is what an implementer hands to their model as structured output. It is not a second source of
truth: every schema here is generated from the same classes the validation gate uses, so the two cannot
disagree.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import JsonSchemaMode, models_json_schema

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import CANDIDATES_SCHEMA, EVIDENCE_BUNDLE_SCHEMA, PROCESSING_TASK_SCHEMA
from boltzmann.exceptions import BlockSchemaError
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.task import PROPOSABLE_MEMORY_TYPES, ProcessingTask
from boltzmann.query.evidence import EvidenceBundle

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
"""The dialect every schema here declares."""

REF_TEMPLATE = "#/$defs/{model}"
"""Where shared definitions live, so the emitted document is self-contained."""


def block_schema(memory_type: MemoryType) -> dict[str, Any]:
    """
    JSON Schema for one memory type's block payload.

    Args:
        memory_type (MemoryType): Which kind of block.

    Returns:
        dict[str, Any]: A self-contained schema, definitions inlined under ``$defs``.

    Raises:
        BlockSchemaError: If no schema is registered for that memory type.
    """
    block_class = _latest(memory_type)
    schema = block_class.model_json_schema(ref_template=REF_TEMPLATE)
    schema["$schema"] = JSON_SCHEMA_DIALECT
    return schema


def candidates_schema(task: ProcessingTask | None = None) -> dict[str, Any]:
    """
    JSON Schema for ``boltzmann.candidates/v1``, with the payload resolved per memory type.

    Args:
        task (ProcessingTask | None): Restrict the schema to the memory types this task allows, so a
            model constrained by it cannot propose what the gate would reject. Defaults to every
            proposable type.

    Returns:
        dict[str, Any]: The schema, ready to hand to a model as structured output.
    """
    allowed = list(task.allowed_memory_types) if task is not None else sorted(PROPOSABLE_MEMORY_TYPES)
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": CANDIDATES_SCHEMA,
        "title": "Boltzmann candidate blocks",
        "description": (
            "What an external model returns for one processing task. A candidate is a proposal, not a "
            "block: it has no identity until the protocol validates and commits it."
        ),
        **_envelope(CandidateSet, {"candidates": {"type": "array", "items": _candidate_variants(allowed)}}),
        "$defs": _definitions(allowed),
    }


def processing_task_schema() -> dict[str, Any]:
    """
    JSON Schema for the task the protocol hands to a model.

    Returns:
        dict[str, Any]: The schema of :class:`~boltzmann.ingest.task.ProcessingTask`.
    """
    schema = ProcessingTask.model_json_schema(ref_template=REF_TEMPLATE)
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["$id"] = PROCESSING_TASK_SCHEMA
    return schema


def evidence_bundle_schema() -> dict[str, Any]:
    """
    JSON Schema for what a query returns.

    Useful to a consumer that wants to validate a bundle it received from another implementation --
    the protocol guarantees the shape even though it leaves the ranking open.

    Returns:
        dict[str, Any]: The schema of :class:`~boltzmann.query.evidence.EvidenceBundle`.
    """
    schema = EvidenceBundle.model_json_schema(ref_template=REF_TEMPLATE)
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["$id"] = EVIDENCE_BUNDLE_SCHEMA
    return schema


def wire_schemas() -> dict[str, dict[str, Any]]:
    """
    Every wire format's schema, keyed by its identifier.

    Returns:
        dict[str, dict[str, Any]]: The schemas an implementer may need to publish alongside an adapter.
    """
    return {
        PROCESSING_TASK_SCHEMA: processing_task_schema(),
        CANDIDATES_SCHEMA: candidates_schema(),
        EVIDENCE_BUNDLE_SCHEMA: evidence_bundle_schema(),
    }


# --- Composition ---------------------------------------------------------------


def _latest(memory_type: MemoryType) -> type[Block]:
    """
    The newest registered schema for a memory type.

    Deliberately the opposite of :meth:`~boltzmann.blocks.base.Block.build`, which picks the
    oldest schema a payload satisfies. The two answer different questions. ``build`` asks what
    a *given* payload is, and the conservative answer keeps a brain readable by older clients.
    This asks what a proposer is *allowed* to send, and the answer has to be the whole surface:
    advertising v1 while the gate accepts v2 would make a field unreachable to every producer
    that learns the shape from here, which is what this module exists to prevent.

    A proposal that uses nothing the newer schema added still round-trips to the older one,
    because ``build`` resolves on the payload rather than on what the schema permitted.
    """
    registry = Block.registry()
    versions = sorted(version for kind, version in registry if kind is memory_type)
    if not versions:
        raise BlockSchemaError(f"no schema registered for {memory_type.value} blocks")
    return registry[(memory_type, versions[-1])]


def _envelope(model: type[CandidateSet], overrides: dict[str, Any]) -> dict[str, Any]:
    """The model's own schema with named properties replaced, and its ``$defs`` dropped."""
    schema = model.model_json_schema(ref_template=REF_TEMPLATE)
    properties = {**schema.get("properties", {}), **overrides}
    body = {
        "type": "object",
        "properties": properties,
        "required": schema.get("required", []),
    }
    if schema.get("additionalProperties") is False:
        body["additionalProperties"] = False
    return body


def _candidate_variants(allowed: Iterable[MemoryType]) -> dict[str, Any]:
    """One variant per allowed memory type, each with its payload pinned to that type's block schema."""
    base = Candidate.model_json_schema(ref_template=REF_TEMPLATE)
    variants = []
    for memory_type in allowed:
        properties = {
            **base.get("properties", {}),
            "memory_type": {"const": memory_type.value, "type": "string"},
            "payload": {"$ref": REF_TEMPLATE.format(model=_latest(memory_type).__name__)},
        }
        variants.append(
            {
                "title": f"{memory_type.value} candidate",
                "type": "object",
                "properties": properties,
                "required": sorted({*base.get("required", []), "memory_type", "payload", "evidence"}),
                "additionalProperties": False,
            }
        )
    return {"oneOf": variants} if len(variants) > 1 else variants[0]


def _definitions(allowed: Iterable[MemoryType]) -> dict[str, Any]:
    """Shared definitions for every block class referenced, plus the candidate set's own."""
    models: list[tuple[type[BaseModel], JsonSchemaMode]] = [(CandidateSet, "validation")]
    models.extend((_latest(memory_type), "validation") for memory_type in allowed)
    _, combined = models_json_schema(models, ref_template=REF_TEMPLATE)
    definitions: dict[str, Any] = dict(combined.get("$defs", {}))
    # The candidate variants are inlined above, so their generated forms would only confuse a reader.
    for redundant in ("Candidate", "CandidateSet"):
        definitions.pop(redundant, None)
    return definitions
