"""Ingestion types: the task, the proposal, the verdict, and the commit report.

The operations are declared on :class:`~boltzmann.protocol.operations.BrainWriter`.
"""

from boltzmann.ingest.commit import CommitResult
from boltzmann.ingest.pipelines import (
    NormalizationPipeline,
    available_pipelines,
    get_pipeline,
    register_pipeline,
)
from boltzmann.ingest.proposer import Candidate, CandidateProposer, CandidateSet
from boltzmann.ingest.register import RegistrationRequest, RegistrationResult
from boltzmann.ingest.schema import (
    block_schema,
    candidates_schema,
    evidence_bundle_schema,
    processing_task_schema,
    wire_schemas,
)
from boltzmann.ingest.task import PROPOSABLE_MEMORY_TYPES, ProcessingTask, TaskOperation
from boltzmann.ingest.validation import (
    ValidatedCandidate,
    ValidationIssue,
    ValidationReport,
    ValidationStatus,
    Validator,
)
from boltzmann.ingest.validators import DEFAULT_VALIDATORS, build_block

__all__ = [
    "PROPOSABLE_MEMORY_TYPES",
    "Candidate",
    "CandidateProposer",
    "CandidateSet",
    "CommitResult",
    "NormalizationPipeline",
    "ProcessingTask",
    "RegistrationRequest",
    "RegistrationResult",
    "TaskOperation",
    "ValidatedCandidate",
    "ValidationIssue",
    "ValidationReport",
    "ValidationStatus",
    "Validator",
    "DEFAULT_VALIDATORS",
    "build_block",
    "block_schema",
    "candidates_schema",
    "evidence_bundle_schema",
    "processing_task_schema",
    "wire_schemas",
    "available_pipelines",
    "get_pipeline",
    "register_pipeline",
]
