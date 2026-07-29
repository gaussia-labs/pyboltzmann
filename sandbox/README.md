# boltzmann-sandbox

A place to run the Boltzmann SDK for real: against an installed wheel, against a real
OCI registry, with the pieces the protocol leaves to the implementer actually
implemented.

Not part of the `boltzmann` distribution. It lives outside the package on purpose and
is excluded from both the wheel and the sdist.

## Why it exists

`boltzmann`'s own test suite is thorough and proves nothing about three things:

1. **The package.** Every test runs against the source tree. A golden vector that
   fails to ship, a missing `py.typed`, an `__init__` that re-exports the wrong name —
   none of it is visible from inside.
2. **A real registry.** The SDK's ORAS tests run against a fake registry. They prove
   the wire conversion, not that a registry accepts an artifact whose `artifactType`
   and config media type are our own.
3. **The implementer's half.** `QueryPlanner` and `Index` ship with no
   implementation, so the code paths that use them — rebuilding indices on commit,
   packing the travelling index into a layer, loading it after a pull — only ever ran
   against test doubles.

This sandbox closes all three, and doubles as a starting point if you are building
against the SDK.

## Before you start

**The server will not start without an OCI artifact to work against.** A brain that
cannot say where it publishes is a brain you cannot test, so this is a hard
requirement, not a default.

```bash
cp .env.example .env
$EDITOR .env
```

### Docker Hub credentials

| Variable | Required | What it is |
|---|---|---|
| `BOLTZMANN_REGISTRY` | **yes** | The artifact: `docker.io/<namespace>/<repo>`. The namespace must be your account or an organization you can write to |
| `DOCKER_USERNAME` | **yes** | Your Docker Hub account |
| `DOCKER_TOKEN` | **yes** | A **Personal Access Token**, not your password |
| `BOLTZMANN_TAG` | no (`latest`) | Tag that push and pull default to |
| `BOLTZMANN_BRAIN_PATH` | no (`./brain`) | The on-disk OCI layout. This directory *is* the brain |
| `BOLTZMANN_ACTOR` | no (`$USER`) | Who registers knowledge; provenance records it on every write |
| `BOLTZMANN_ANONYMOUS` | no (`0`) | `1` to talk to a registry with no credentials |
| `BOLTZMANN_INSECURE` | no (`0`) | `1` to allow plain HTTP; local registries only |

To create the token: **Docker Hub → Account settings → Personal access tokens →
Generate new token**, scope **Read & Write**. A read-only token cannot push. Docker
Hub creates the repository on first push, so you do not need to make it beforehand.

### What bites you on Docker Hub

None of these are SDK bugs, and all three show up on the first real run:

- The free tier allows **one** private repository. A second one has to be public.
- Pulls are rate-limited per account.
- The artifact shows in the Hub UI as an unrecognized type, because it is not a
  container image. `docker pull` on it will fail, correctly — use `pull_brain`, or
  `oras pull`.

## Running it

```bash
uv sync                      # builds and installs the SDK from ../ , plus fastmcp

uv run boltzmann-doctor      # before anything else: what is missing, and does the registry answer
uv run boltzmann-mcp         # the MCP server, over stdio
uv run boltzmann-demo        # the whole lifecycle, no MCP, with assertions
uv run pytest                # the sandbox's own tests
```

`boltzmann-doctor` is what enforces "name the artifact before starting". It checks the
variables, resolves the reference, authenticates, reports whether the repository is
writable, and warns when the installed `boltzmann` has fallen behind `../src`. It
exits non-zero if anything would prevent the server from starting. The server
validates the same things in its lifespan, so there is no way to start it
misconfigured.

For HTTP instead of stdio: `uv run boltzmann-mcp --http --port 8000`.

### Without a Docker account

A local registry needs no credentials and no network:

```bash
docker run -d -p 5000:5000 --name boltzmann-registry registry:2

BOLTZMANN_REGISTRY=localhost:5000/demo/brain \
BOLTZMANN_INSECURE=1 BOLTZMANN_ANONYMOUS=1 \
  uv run boltzmann-demo
```

Same code either way — the demo publishes to whatever `BOLTZMANN_REGISTRY` names.
Useful for iterating, but note that `registry:2` is the reference implementation and
therefore the *easy* case: it accepting the artifact does not tell you Docker Hub
will.

## Adding it to an MCP client

```bash
claude mcp add boltzmann -- uv run --directory /path/to/sandbox boltzmann-mcp
```

The tools map one-to-one onto the protocol's operations. Ingestion is deliberately
two calls:

1. `open_task` returns a `ProcessingTask` **and the JSON Schema** its candidates must
   satisfy — the schema the SDK emits, not a description of one.
2. Your model writes candidates against that schema.
3. `submit_candidates` validates and commits them.

That shape is the protocol's boundary made visible: the model proposes, and there is
no tool that lets it write to a Merkle DAG. Validation and commit happen server-side
or not at all.

## The SDK installs as a built wheel, not an editable

`[tool.uv.sources]` points `boltzmann` at `..` with `editable = false`. That is the
point: the sandbox exercises the *packaged* SDK, which is how a missing data file
becomes visible. The cost is that editing `../src/boltzmann` does not propagate until

```bash
uv sync --reinstall-package boltzmann
```

`boltzmann-doctor` warns when the installed copy is older than the sources. When the
SDK is published, delete the `[tool.uv.sources]` block; nothing else changes.

## What is implemented here that the SDK leaves open

Deliberately simple, no new dependencies. These are examples, not recommendations.

- **`InvertedIndex`** — term postings with idf-weighted overlap. Rebuildable, like
  every structural index.
- **`VectorIndex`** — feature hashing into 256 dimensions, cosine similarity. Reports
  `rebuildable = False` and satisfies `TravellingIndex`, so it ships inside its
  module's layer and records the model that produced it
  (`sandbox-hashing-bow/1`). This is the index that exercises §6.3 end to end,
  including the refusal to load one built by a different model. The similarity is
  lexical in disguise — swap in real embeddings if you want it to mean more.
- **`HybridPlanner`** — fuses three rankings with Reciprocal Rank Fusion, and
  delegates filtering, resolution and verification to the SDK's own scan. Candidate
  generation and ranking are the planner's; verification stays with the protocol.

  It does not make retrieval faster — the scan is still linear. It makes ranking
  better and exercises the fusion path. A production planner would generate
  candidates from the index and verify without traversing everything.
