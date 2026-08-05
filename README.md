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
brain = Brain.open("./my-brain", actor=curator)
brain.ingest(pdf, request, my_llm)          # register → delegate → validate → commit
brain.search(Query(text="Fourier"))         # filter, resolve, verify
brain.drop(DropRequest(...))                # rebuild the Merkle DAG, cascade, record
await brain.push(client, "ghcr.io/org/brain", "v1")
```

The line it draws: **the SDK does whatever the protocol defines mechanically; the
implementer supplies whatever the paper assigns elsewhere.** So identity, the wire
formats, the four operation paths and a conformance suite are here; the model, the
ranking, the index engines and any CLI or MCP server are yours.

It embeds no language model. Interpretation enters through `CandidateProposer` and
nowhere else.

There are **no `NotImplementedError` stubs**, and a test enforces it. An
unimplemented function is worse than an interface: it looks callable and is not.
Nothing is declared and unreachable either — every type, enum member and constant
is produced by something, and a test enforces that too.

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

The core needs **`pydantic` and `rfc8785`**. Everything else is optional: `[oci]`
adds a network registry transport, and moving a brain between OCI layouts on disk
needs nothing at all.

Python >= 3.11.

## Usage

The whole lifecycle of Section 11, against a real OCI layout:

```python
from boltzmann import Actor, Brain, MemoryType, Producer, Query
from boltzmann.blocks import ActorKind, ProducerKind
from boltzmann.ingest import Candidate, CandidateSet, RegistrationRequest

curator = Actor(id="curator", kind=ActorKind.HUMAN)
brain = Brain.open("./my-brain", actor=curator)

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

request = RegistrationRequest(media_type="application/pdf", actor=curator, license="CC-BY-4.0")
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

Removing knowledge cascades through provenance, publishing is a copy rather than a
conversion, and an implementation in any language can prove it conforms against the
golden vectors that ship in the wheel. Each of those has a guide.

## Documentation

The [`docs/`](https://github.com/gaussia-labs/pyboltzmann/tree/master/docs) directory
is the source of truth, and it is published as the Boltzmann SDK section of the
[Gaussia docs](https://github.com/gaussia-labs/docs).

<!-- Absolute URLs: this file is also the PyPI long description, where a relative link
     resolves against pypi.org and 404s. -->

| | |
|---|---|
| [Quickstart](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/quickstart.mdx) | Ingest, query, prove, publish, remove — in one file |
| [Architecture](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/concepts/architecture.mdx) | Blocks, compositions, modules, snapshots |
| [Memory types](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/concepts/memory-types.mdx) | The five typed blocks and the rules each obeys |
| [Identity](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/concepts/identity.mdx) | JCS, the three levels of hashes, the values a payload refuses |
| [Merkle DAGs](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/concepts/merkle.mdx) | RFC 9162 over sorted leaves, and inclusion proofs |
| [Interfaces](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/concepts/interfaces.mdx) | The protocol surface, and the four things you plug in |
| [Ingestion](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/guides/ingestion.mdx) | Preserve the source, delegate the interpretation, validate |
| [Query](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/guides/query.mdx) | Evidence Bundles, filters, and supplying a planner |
| [Retention](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/guides/retention.mdx) | Drop, supersede, demote, prune, redact |
| [Distribution](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/guides/distribution.mdx) | Pack, push, pull, and selective installs |
| [Conformance](https://github.com/gaussia-labs/pyboltzmann/blob/master/docs/guides/conformance.mdx) | Golden vectors, and the suites you inherit |

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

MIT — see [LICENSE](https://github.com/gaussia-labs/pyboltzmann/blob/master/LICENSE).
