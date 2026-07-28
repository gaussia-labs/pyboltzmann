"""Package-wide constants.

This module holds only literals and imports nothing, so every layer may depend on it without
inverting the dependency direction of the SDK. The protocol version and the wire schema names live
here rather than under :mod:`boltzmann.protocol` because the kernel needs them -- a block envelope
carries the protocol version -- and the kernel must not depend on the protocol layer.

They are re-exported from :mod:`boltzmann.protocol` for callers, which is where they belong
conceptually.
"""

PROTOCOL_VERSION = 1
"""Version of the Boltzmann Protocol this SDK implements. Stored in every block envelope."""

CANDIDATES_SCHEMA = "boltzmann.candidates/v1"
"""Output schema an external LLM must satisfy when proposing blocks (paper Section 8.2)."""

EVIDENCE_BUNDLE_SCHEMA = "boltzmann.evidence/v1"
"""Schema of the data contract a query returns (paper Section 9.3)."""

PROCESSING_TASK_SCHEMA = "boltzmann.task/v1"
"""Schema of the processing task the protocol hands to an external LLM (paper Section 8.2)."""
