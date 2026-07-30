# boltzmann-sandbox

A place to run the Boltzmann SDK for real: against an installed wheel, against a real
OCI registry, with the pieces the protocol leaves to the implementer actually
implemented.

Not part of the `pyboltzmann` distribution. It lives outside the package on purpose and
is excluded from both the wheel and the sdist.

## Why it exists

The SDK's own test suite is thorough and proves nothing about three things:

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

### What it has already caught

Seven real defects. Not one was visible from inside the SDK's own suite, and the last
three needed a hosted registry — `registry:2` is the reference implementation and
therefore the easy case.

| Where | What |
|---|---|
| `pyproject.toml` | The sdist's `include` patterns were unanchored, so hatchling matched `README.md` by basename at any depth and shipped `sandbox/README.md` inside the distribution |
| `Brain` | Indices were rebuilt only by the write path. A brain reopened in a new process, or a version installed from a registry, held empty indices — and `plan_pull` was already reporting `rebuild_indices` with nothing acting on it |
| `VectorIndex` | Vectors were rounded on the way out, so the publisher ranked with full precision and a consumer that loaded the index ranked with six decimals. The dumps matched, so it was invisible until a near-tie |
| `oras-py` | It shells out to the Docker credential helper with no timeout. A helper that blocks — on macOS the first call can wait on a keychain dialog no headless process will ever see — takes the whole run with it, silently. Worked around in `brain.py` |
| **`push`** | **The fast-forward check failed open.** It read any failure to resolve the remote as "nothing is published here", so an expired token or a 500 became permission to overwrite a version it could not see |
| `resolve` | Discarded the HTTP status, so a JSON parse error was all a caller got when the request had in fact reached the wrong host entirely |
| `push` | Requested exactly the token scope the registry's challenge advertised, which for Docker Hub's upload endpoint is `pull` |

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

Write `BOLTZMANN_REGISTRY` as `docker.io/<namespace>/<repo>`, the way you would to
`docker pull`. The sandbox substitutes `registry-1.docker.io` before talking to the
registry, because `docker.io` is the index hostname and serves the website — the
Docker CLI does the same substitution for you, quietly.

**A prior `docker login` does not authenticate this sandbox.** The credentials have to
be in the environment. That is deliberate: reading them from `~/.docker/config.json`
means letting ORAS invoke the platform credential helper, which it does with no
timeout — see `_ignore_docker_config` in `boltzmann_sandbox/brain.py`.

### What bites you on Docker Hub

None of these are SDK bugs, and all three show up on the first real run:

- The free tier allows **one** private repository. A second one has to be public.
- Pulls are rate-limited per account.
- The artifact shows in the Hub UI as an unrecognized type, because it is not a
  container image. `docker pull` on it will fail, correctly — use `pull_brain`, or
  `oras pull`. Docker Hub does classify it as an ARTIFACT; what it cannot do is
  render a type it has never heard of, which is what `boltzmann-inspect` is for.

## Running it

```bash
uv sync                      # builds and installs the SDK from ../ , plus fastmcp

uv run boltzmann-doctor      # before anything else: what is missing, and does the registry answer
uv run boltzmann-mcp         # the MCP server, over stdio
uv run boltzmann-demo        # the whole lifecycle, no MCP, with assertions
uv run boltzmann-inspect v1  # what a published brain contains, without downloading it
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

## What is verified, and what is not

**Verified** — the full lifecycle against a local `registry:2`, every step asserted:

```
1. Register and ingest        3 blocks from one source
2. Re-register                same identity, dedup is a no-op
3. Search                     hybrid ranking, all_verified
4. Prove membership           1 hash for a tree of 3, verifies
5. The travelling index       vector=False  inverted=True
6. Replace the source         both still members
7. Publish                    4 layers + config
8. Install into an empty brain    same digest as published
9. The index arrived          byte-identical dump, model tag intact
10. Search the installed brain    verified
11. Drop the evidence         privileged, cascades to all 3, all re-derivable
12. Older roots               still verify
13. Prune                     5 blobs reclaimed, both brains still verify
```

**Verified: Docker Hub accepts a Boltzmann brain.** The same thirteen steps ran
against `registry-1.docker.io/<namespace>/boltzmann-sandbox:v1` with a Personal
Access Token, and the installed version carried the digest that was published.
A manifest with `artifactType: application/vnd.gaussia.boltzmann.brain.v1+json`
and a config media type of `application/vnd.gaussia.boltzmann.snapshot.v1+json`
is accepted with `201 Created`, and the vector index layer comes back with its
`ai.gaussia.boltzmann.embedding-model` annotation intact.

Getting there took three fixes, all in the client and none in the protocol. They are
in the table above; the two that only a hosted registry can expose are worth
restating, because anyone building another Boltzmann client over `oras-py` will meet
them:

- **`docker.io` is not the registry.** It is the index hostname, and it serves the
  Docker Hub *website*. A request to `https://docker.io/v2/…` returns 200 with HTML.
- **The upload endpoint's challenge advertises `pull` alone.** A client that honours
  `Www-Authenticate` literally gets a read-only token and is then refused by the same
  registry, whose error names `pull` and `push`. The `docker` CLI never hits this
  because it asks for `pull,push` when it intends to push.

## A known limitation

**A reopened brain has an empty travelling index.** The structural indices are rebuilt
on open, but the vector index cannot be: rebuilding it is what `rebuildable = False`
denies. Its bytes are in the store after a pull, and its digest is not recoverable
from the snapshot — only the manifest names index layers, and `pull` does not write
the manifest into the layout's `index.json` the way `pack` does.

The effect is bounded: retrieval loses one of three rankings, so results are ranked
worse but never wrong, and nothing about verification changes. Two ways out, both
worth deciding on rather than drifting into — have `pull` record the manifest so
`open` can find the index layers, or have `ModuleRef` carry the index digest next to
`embedding_model`.

## Adding it to an MCP client

```bash
claude mcp add boltzmann -- uv run --directory /path/to/sandbox boltzmann-mcp
```

Eighteen tools, one per protocol operation. Read tools carry `readOnlyHint`; `drop`
and `prune` carry `destructiveHint`, and `drop` additionally refuses without
`confirm=true`. Ingestion is deliberately two calls:

1. `open_task` returns a `ProcessingTask` **and the JSON Schema** its candidates must
   satisfy — the schema the SDK emits, not a description of one.
2. Your model writes candidates against that schema.
3. `submit_candidates` validates and commits them.

That shape is the protocol's boundary made visible: the model proposes, and there is
no tool that lets it write to a Merkle DAG. Validation and commit happen server-side
or not at all.

## The SDK installs as a built wheel, not an editable

`[tool.uv.sources]` points `pyboltzmann` at `..` with `editable = false`. That is the
point: the sandbox exercises the *packaged* SDK, which is how a missing data file
becomes visible. The cost is that editing `../src/boltzmann` does not propagate until

```bash
uv sync --reinstall-package pyboltzmann
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
