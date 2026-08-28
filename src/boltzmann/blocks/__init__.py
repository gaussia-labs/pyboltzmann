"""Typed knowledge blocks: the durable unit of the protocol."""

from boltzmann.blocks.base import ENVELOPE_KEYS, Block
from boltzmann.blocks.canonical import CanonicalBlock, NormalizedView
from boltzmann.blocks.content import ContentRef, NamesContent, require_media_type
from boltzmann.blocks.episodic import EpisodicBlock, EpisodicBlockV2
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.procedural import ProceduralBlock, ProceduralBlockV2, Step
from boltzmann.blocks.provenance import (
    Actor,
    ActorKind,
    DemotionRecord,
    DerivationRecord,
    NormalizationRecord,
    Producer,
    ProducerKind,
    ProvenanceBlock,
    ProvenanceEntry,
    RegistrationRecord,
    RemovalMechanism,
    RemovalRecord,
    SupersessionRecord,
)
from boltzmann.blocks.semantic import Relation, SemanticBlock, SemanticBlockV2, SemanticBlockV3, SemanticKind

__all__ = [
    "ENVELOPE_KEYS",
    "Actor",
    "ActorKind",
    "Block",
    "CanonicalBlock",
    "ContentRef",
    "DemotionRecord",
    "DerivationRecord",
    "EpisodicBlock",
    "EpisodicBlockV2",
    "MemoryType",
    "NamesContent",
    "NormalizationRecord",
    "NormalizedView",
    "ProceduralBlock",
    "ProceduralBlockV2",
    "Producer",
    "ProducerKind",
    "ProvenanceBlock",
    "ProvenanceEntry",
    "RegistrationRecord",
    "Relation",
    "RemovalMechanism",
    "RemovalRecord",
    "SemanticBlock",
    "SemanticBlockV2",
    "SemanticBlockV3",
    "SemanticKind",
    "Step",
    "SupersessionRecord",
    "require_media_type",
]
