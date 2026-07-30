# pyboltzmann

An SDK for the **Boltzmann Protocol**: portable, verifiable, model-agnostic knowledge.

> *The brain conserves, validates, and retrieves knowledge. An external LLM
> processes, contextualizes, and uses it.*

Reference: [*Boltzmann Brain: A Versioned, Distributable, and Model-Agnostic
Knowledge Architecture*](https://github.com/gaussia-labs/papers) (Gaussia, 2026).

## What this is

A **client** for a Boltzmann brain. You open a directory, call methods, and they
work against an OCI artifact. `Brain` implements the whole protocol — 23 of 23
operations across the four contracts.

```python
brain = Brain.open("./my-brain", actor=alex)
brain.ingest(pdf, request, my_llm)          # register → delegate → validate → commit
brain.search(Query(text="Fourier"))         # filter, resolve, verify
brain.drop(DropRequest(...))                # rebuild the Merkle DAG, cascade, record
await brain.push(client, "ghcr.io/org/brain", "v1")
```

The line it draws: **the SDK does whatever the protocol defines mechanically; the
implementer supplies whatever the paper assigns elsewhere.**

| The SDK does | Because |
|---|---|
| Identity: canonical serialization, `block_id`, Merkle roots, inclusion proofs | Two clients that disagree on these do not share a brain at all |
| The wire formats, and their JSON Schema | Two clients that disagree cannot hand work to the same model |
| Ingestion, query, retention, distribution | You should not have to write hashing, cascades and mark-and-sweep yourself |
| A conformance suite and golden vectors | So an implementation can prove it conforms, in any language |

| You supply | Because |
|---|---|
| `CandidateProposer` | What knowledge a source yields is the external model's judgment (Principle 5) |
| `QueryPlanner` | Ranking and index selection are explicitly implementation-defined (§9.2) |
| `Index` engines | Which engine backs an index is the implementation's choice (§6.3) |
| An MCP server or CLI | Exposure layers, not protocol — build them on top |

There are **no `NotImplementedError` stubs**, and a test enforces it. An
unimplemented function is worse than an interface: it looks callable and is not.
Nothing is declared and unreachable either — every type, enum member and constant
is produced by something, and a test enforces that too.

The core needs **`pydantic` and `rfc8785`**. Everything else is optional: `[oci]`
adds a network registry transport, and moving a brain between OCI layouts on disk
needs nothing at all.

## What you plug in

```python
from boltzmann import CandidateProposer, QueryPlanner, Index, Validator, BlockStore
from boltzmann.indices import TravellingIndex
from boltzmann.distribution import RegistryClient
from boltzmann.ingest import NormalizationPipeline
from boltzmann.merkle import MerkleLayout

brain = Brain.open(
    "./my-brain",
    actor=alex,
    planner=MyPlanner(),                              # ranking
    indices={MemoryType.SEMANTIC: [MyVectorIndex()]}, # engines
    validators=[*DEFAULT_VALIDATORS, MyDomainCheck()],
)
```

Every interface is `runtime_checkable`, so conformance is asserted rather than
hoped for:

```python
assert isinstance(my_client, BrainReader)
```

The protocol surface is split because *read* and *extend* are separable, and most
consumers only read: a client satisfying `BrainReader` is conforming for what it
claims, without pretending to support writes it will refuse.

An index that reports `rebuildable = False` must satisfy `TravellingIndex` — it
ships with its module, because no client can regenerate it (§6.3).

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
pip install pyboltzmann          # the distribution
pip install 'pyboltzmann[oci]'   # plus the network registry transport
```

```python
import boltzmann                 # the import package
```

The two names differ because `boltzmann` on PyPI belongs to an unrelated package. It is the same split as
`pygaussia` providing `gaussia`.

## Usage

The whole lifecycle of Section 11, against a real OCI layout:

```python
from boltzmann import Actor, Brain, MemoryType, Producer, Query
from boltzmann.blocks import ActorKind, ProducerKind
from boltzmann.ingest import Candidate, CandidateSet, RegistrationRequest
from boltzmann.retention import DropRequest

alex = Actor(id="alex", kind=ActorKind.HUMAN)
brain = Brain.open("./my-brain", actor=alex)

# You supply the model. The SDK embeds none: what knowledge a source yields is its
# judgment, and what gets stored is the protocol's.
def my_llm(task, source):
    # task.output_schema names the schema; brain.candidates_schema(task) *is* it, with the
    # payload resolved per memory type. Hand it to the model as structured output.
    return CandidateSet(
        producer=Producer(kind=ProducerKind.MODEL, id="claude-opus-5", version="2026-07"),
        candidates=[
            Candidate(
                memory_type=MemoryType.SEMANTIC,
                evidence=[task.source],
                locator="p.147",
                payload={
                    "kind": "formula",
                    "label": "Fourier series",
                    "statement": "decomposes a periodic function into sines",
                    "subject": "signals",
                },
            )
        ],
    )

request = RegistrationRequest(media_type="application/pdf", actor=alex, license="CC-BY-4.0")
pdf = b"%PDF-1.7 lecture 07: Fourier analysis"

# Register, delegate, validate, commit. Registering the same source twice is a no-op.
commit = brain.ingest(pdf, request, my_llm)

# Data with its provenance, never prose, every match verified against the snapshot.
bundle = brain.search(Query(text="periodic function"))
assert bundle.all_verified
assert bundle.matches[0].sources[0].locator == "p.147"

# Membership is provable in O(log n), without holding the rest of the module.
block_id = commit.committed[0]
assert brain.prove(block_id, MemoryType.SEMANTIC).verify(brain.root_of(MemoryType.SEMANTIC))
assert brain.verify()
```

Removing knowledge, with the cascade the paper requires:

```python
source = brain.module(MemoryType.CANONICAL).block_ids[0]

# Ask first: a canonical drop is privileged and always cascades to what cited it.
plan = brain.plan_drop(DropRequest(
    blocks=[source], memory_type=MemoryType.CANONICAL, actor=alex, reason="ingested in error",
))
print(plan.privileged, plan.size)      # True, and how many derived blocks go with it

# Off by default: excluding evidence forfeits re-derivation from it.
from boltzmann.retention import RetentionPolicy
brain = Brain.open("./my-brain", actor=alex, policy=RetentionPolicy(canonical_drop_allowed=True))
result = brain.drop(DropRequest(
    blocks=[source], memory_type=MemoryType.CANONICAL, actor=alex, reason="ingested in error",
))
# One commit, several new roots. Older retained roots keep verifying exactly as before.

brain.prune(dry_run=False)             # reclaim what no retained root needs
```

Publishing and installing:

```python
from boltzmann.distribution import LocalLayoutRegistry, OrasRegistryClient

registry = OrasRegistryClient()                    # or LocalLayoutRegistry("./registry")
await brain.push(registry, "ghcr.io/org/brain", "v1")

# Selective install: one module, and the layers already held are reused by digest.
consumer = Brain.open("./local", actor=alex)
plan = await consumer.plan_pull(registry, "ghcr.io/org/brain", "v1")   # costs one manifest
await consumer.pull(registry, "ghcr.io/org/brain", "v1", modules=[MemoryType.SEMANTIC])
```

A push refuses to overwrite a remote whose snapshot is absent from the local history:
the paper defines no merge for divergent brains, so the safe move is to say where the
two parted. `brain.pack(tag="v1")` materializes the artifact locally with no network
at all — the directory becomes a real OCI artifact any tool can copy.

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
