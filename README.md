# pyboltzmann

An SDK for the **Boltzmann Protocol**: portable, verifiable, model-agnostic knowledge.

> *The brain conserves, validates, and retrieves knowledge. An external LLM
> processes, contextualizes, and uses it.*

Reference: [*Boltzmann Brain: A Versioned, Distributable, and Model-Agnostic
Knowledge Architecture*](https://github.com/gaussia-labs/papers) (Gaussia, 2026).

## What this is, and what it is not

This is a **protocol SDK**. It is not a brain, and it does not ship one.

The line it draws is simple: **implemented is whatever every conforming client must
compute identically; declared is whatever the paper leaves to the implementation.**

| The SDK provides | Because |
|---|---|
| Canonical serialization, `block_id`, Merkle roots, inclusion proofs | Two clients that disagree on these do not share a brain at all |
| Block schemas and the wire formats | Two clients that disagree on these cannot hand work to the same model |
| The interfaces an implementation satisfies | So "conforming" is checkable, not aspirational |
| A conformance suite and golden vectors | So an implementation can prove it conforms, in any language |

| The SDK does not provide | Because |
|---|---|
| Ingestion, query, retention, distribution *operations* | Declared as `Protocol`s; implementing them is the implementer's job |
| Index engines | Which engine backs an index is explicitly the implementation's choice (§6.3) |
| A query planner or a fusion method | Explicitly implementation-defined (§9.2) |
| A CLI, MCP server, or skill | Exposure layers, not protocol |
| Any LLM adapter | Principle 5. Interpretation enters through `CandidateProposer` and nowhere else |

There are **no `NotImplementedError` stubs**, and a test enforces that. An
unimplemented function is worse than an interface: it looks callable and is not.

Consequence worth stating: the SDK has **zero optional dependencies** — just
`pydantic` and `rfc8785`.

## The interfaces you implement

```python
from boltzmann import BrainReader, BrainWriter, BrainRetention, BrainDistribution
from boltzmann import BlockStore, Index, QueryPlanner, Validator, CandidateProposer
from boltzmann.distribution import RegistryClient
from boltzmann.ingest import NormalizationPipeline
from boltzmann.merkle import MerkleLayout
```

The protocol surface is split because *read* and *extend* are separable, and most
consumers only read. A client that satisfies `BrainReader` is conforming — it does
not have to pretend to support writes it will refuse. `BoltzmannProtocol` composes
all four for an implementation that offers everything.

Every interface is `runtime_checkable`, so an implementer can assert conformance
rather than hope for it:

```python
assert isinstance(my_client, BrainReader)
```

The SDK ships a reference for exactly two of them: `BlockStore`
(`MemoryBlockStore`, `OciLayoutStore`) because the kernel and the conformance suite
need something to run against, and `MerkleLayout` (`SortedRfc6962Layout`) because
roots must be identical across clients.

## Decisions this SDK closes

The paper deliberately leaves these open (§12). An SDK cannot.

- **Canonical serialization**: JCS (RFC 8785), tagged `"jcs/1"` in every envelope so
  it stays versionable. Chosen over a binary encoding because a block is a small
  record and the protocol targets several languages: a canonical form a human can
  read and `grep` beats compactness here.
- **Floats and unsafe integers are refused inside a payload.** JCS defines float
  serialization through ECMAScript rules that are hard to reproduce identically
  across languages, and integers outside the IEEE-754 safe range lose precision in
  any double-backed JSON parser. Either divergence would mean two conforming clients
  computing different `block_id` values for the same knowledge.
- **Merkle layout**: RFC 6962 over lexicographically sorted leaves, behind
  `MerkleLayout`. Sorting makes the root a pure function of the *set* of blocks;
  RFC 6962 avoids the duplicate-leaf ambiguity a naive tree admits (CVE-2012-2459).
  Internal nodes are derived, not stored.
- **On-disk format**: the local brain *is* an OCI Image Layout, so publishing is a
  copy rather than a conversion, and selective installation falls out of the layout.
- **Three levels of hashes are three types.** `BlockId`, `MerkleRoot`, `OciDigest` —
  none is a `str`, and none is interchangeable with another.

## Invariants made structural

The paper states these as rules. Here they are errors, each with a test in
`tests/test_invariants.py`:

- A `Candidate` is not a `Block` and has no `block_id` — an unvalidated proposal has
  no identity, so it cannot be committed by accident.
- `ProcessingTask` refuses to let a model propose canonical or provenance blocks.
- `Module` exposes no write method; deriving returns a new module.
- `EvidenceBundle` has no answer field. Not omitted — absent by design.
- `Composition.drop()` on the episodic module raises, and **no policy can permit it**.
- `RetentionPolicy.record_removals` is a property that is always `True` — no
  configuration turns auditability off.
- A `float` in a payload fails at construction.

## Requirements

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/)

## Installation

```bash
pip install boltzmann
```

## Usage

Working with the parts that are implemented — identity, blocks, compositions,
verification:

```python
from boltzmann import CanonicalBlock, MemoryType, Module, OciLayoutStore, SemanticBlock, Snapshot
from boltzmann.blocks import SemanticKind
from boltzmann.module import Composition

store = OciLayoutStore("./my-brain")

# Preserve evidence. The canonical block is a statement about the bytes and nothing
# else, so registering the same source twice yields the same identity and adds nothing.
pdf = b"%PDF-1.7 lecture 07: Fourier analysis"
evidence = CanonicalBlock(blob=store.put_bytes(pdf), media_type="application/pdf", size=len(pdf))

# An interpretation of it, citing it as evidence.
concept = SemanticBlock(
    kind=SemanticKind.FORMULA,
    label="Fourier series",
    statement="f(x) = a0/2 + sum(a_n cos(nx) + b_n sin(nx))",
    evidence=[evidence.block_id],
)
for block in (evidence, concept):
    store.put_block(block)

semantic = Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [concept.block_id]))
snapshot = Snapshot.of([semantic.reference()])

assert semantic.verify()
assert semantic.inclusion_proof(concept.block_id).verify(semantic.root)
```

## Conformance

An implementation in any language must reach the same identities. The golden vectors
ship in the wheel as plain JSON for exactly that:

```python
from boltzmann.conformance import golden

for vector in golden.load("block_ids.json")["vectors"]:
    assert my_implementation.block_id(vector["envelope"]) == vector["block_id"]
```

A Python implementation can inherit the behavioral suite directly:

```python
from boltzmann.conformance import BlockStoreConformance


class TestMyStore(BlockStoreConformance):
    def make_store(self):
        return MyStore()
```

## Development

```bash
uv sync
uv run pre-commit install && uv run pre-commit install --hook-type commit-msg

uv run ruff check . && uv run ruff format .
uv run mypy src
uv run pytest
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) — use
`uv run cz commit` for the interactive prompt. Releases are cut by
`python-semantic-release` from the commit history.

## License

MIT — see [LICENSE](LICENSE).
