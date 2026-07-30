# Using the sandbox

A walkthrough of the things you actually do with a brain: put knowledge in, get it out,
publish it, install it somewhere else, and take things back out. Every command here was run
against a real Docker Hub repository, and the outputs are real.

The [README](../README.md) covers why the sandbox exists and how to configure it. This is
what to do once it starts.

## Contents

1. [The four entry points](#the-four-entry-points)
2. [Putting knowledge in](#putting-knowledge-in)
3. [Getting knowledge out](#getting-knowledge-out)
4. [Proving what you got](#proving-what-you-got)
5. [Publishing and installing](#publishing-and-installing)
6. [Taking knowledge back out](#taking-knowledge-back-out)
7. [Looking at an artifact](#looking-at-an-artifact)
8. [When something goes wrong](#when-something-goes-wrong)

## The four entry points

```bash
uv run boltzmann-doctor        # before anything: is the environment usable
uv run boltzmann-mcp           # the MCP server, over stdio
uv run boltzmann-demo          # the whole lifecycle, self-checking, no MCP
uv run boltzmann-inspect v1    # what a published brain contains
```

Run `boltzmann-doctor` first, every time something behaves oddly. It answers the questions
that otherwise turn into confusing failures deep inside a tool call:

```
[  ok  ] pyboltzmann installed    0.1.0 from site-packages/
[  ok  ] boltzmann is current     matches ../src/boltzmann
[  ok  ] artifact                 docker.io/you/brain:v1 -> registry-1.docker.io/you/brain:v1
[  ok  ] credentials              you (token present)
[  ok  ] local brain              sha256:5494b10f3269 -- 4 modules, 34 blocks, 2 versions
[  ok  ] travelling index         present for procedural, semantic
[  ok  ] registry login           registry-1.docker.io accepted you
[  ok  ] remote tag               …:v1 resolves -- layers: canonical, procedural, provenance, semantic
```

`boltzmann-demo` is the fastest way to see whether the whole thing works after a change. It
runs the paper's Section 11 lifecycle with a fixed proposer and **asserts at every step**, so
a green run means something. It uses its own brain directories and its own tag, and never
touches the brain you work in.

## Putting knowledge in

Ingestion is two calls, and that is the design rather than a limitation. The brain does not
decide what a document means — an external model does, and the protocol validates what it
proposes. So the boundary is visible in the API: there is no tool that lets a model write to
a Merkle DAG.

### 1. Register the source

The bytes are preserved verbatim and addressed by their hash. This is canonical memory: what
was actually observed, so every later claim can be traced back to it.

```
register_source(media_type="application/x-tex", file_path="./paper.tex", license="CC-BY-4.0")

  { "block_id": "sha256:9f1cfbf0ff19…", "duplicate": false, "size": 76381 }
```

Registering the same bytes again is a no-op that returns the same identity, so this is safe
to retry:

```
  { "block_id": "sha256:9f1cfbf0ff19…", "duplicate": true, "snapshot": null }
```

### 2. Open a task and read the schema

```
open_task(source="sha256:9f1cfbf0ff19…", allowed=["semantic", "procedural"],
          instructions="Extract the normative claims. Use the section number as the locator.")
```

It returns the processing task **and the JSON Schema its candidates must satisfy** — the
schema the SDK emits, not a description of one. Hand it to your model as structured output.
With one allowed memory type the schema names that variant directly; with several it uses
`oneOf`:

```
  candidates_schema.properties.candidates.items.oneOf → [procedural, semantic]
  $defs → EpisodicBlock, MemoryType, Producer, Relation, SemanticBlock, SemanticKind …
```

Canonical and provenance can never be proposed. One is the source itself; the other is the
brain's own record of what happened.

### 3. Submit the candidates

```
submit_candidates(task_id="task-f43f546cc275", producer_id="claude-opus-5",
                  producer_version="2026-07", candidates=[…])

  { "validation": { "counts": { "validated": 16 } },
    "committed": [ "sha256:…", … ],
    "snapshot":  "sha256:fce2614dc69b" }
```

Validation is the brain's, not yours. A candidate that cites evidence it was not derived
from, duplicates a block already present, or contradicts one, comes back **rejected with a
code** rather than stored — and rather than raised as an error:

```
  rejected  publish a brain
            ('schema', 'payload does not satisfy the schema: 4 validation errors … steps.0.order')

  rejected  Boltzmann Brain
            ('duplicate', 'block sha256:fa50d91df7ca is already in the semantic composition')

  rejected  x
            ('evidence-not-found', 'cited evidence sha256:ababab… is not in the canonical composition')
```

A rejection is information: fix the candidate and submit again. The duplicate case is worth
understanding — identical knowledge *is* identical, so re-submitting a set you already
committed rejects all of it and commits nothing. That is correct, not a failure.

### Pack before the process exits

If you ingest and the process ends without packing or pushing, the **vector index is lost**.
It cannot be regenerated — that is what a travelling index means — and it is persisted only
when the artifact is materialized. `boltzmann-doctor` warns:

```
[ warn ] travelling index    absent for semantic -- a push from this process would publish
                             those modules without their vector index
```

So: `push_brain` from the process that committed, or call `pack_local` before it exits.

## Getting knowledge out

A query says what you want and never names an index. Choosing and combining indices is the
implementation's job.

```
search(text="why can a client not rebuild the vector index", limit=3)
```

```
  1.0000  [semantic] the vector index travels
          Every index except the vector index is a deterministic function of the blocks…
          cita sha256:9f1cfbf0ff19…  @ S6.3
          verified=True  resolvable=True
```

What comes back is an **Evidence Bundle**: blocks with their provenance and a score. There is
no answer field, by design — composing prose is your work, and citing `block_id` is what
keeps the answer checkable.

### The shapes of a query

| What you want | How |
|---|---|
| Natural language | `search(text="how is a version identified")` |
| One kind of memory | `search(text="publish", memory_types=["procedural"])` |
| One domain | `search(subject="retention")` — no text at all |
| A specific block | `search(text="sha256:e183018c68…", mode="exact")` |
| Including replaced blocks | `search(text="…", include_superseded=True)` |
| Following relations outward | `search(text="…", expand_depth=2)` |

Filters work with no text: `search(memory_types=["procedural"])` returns everything
procedural. A recency window (`since`, `until`) applies to episodic memory.

`mode` names a *strategy*, never an engine: `auto`, `exact`, `lexical`, `semantic`,
`associative`. A conforming implementation may ignore it and still be conforming.

### What retrieval here is, and is not

The ranking is deliberately crude and the sandbox says so. Term matching with a small
stemmer, plus hashing bag-of-words vectors, fused with Reciprocal Rank Fusion. It gets the
right answer first on straightforward questions and it will not find a synonym:

```
'what happens when you remove the source of a claim'  →  privileged cascade      1.0000
'thermodynamic entropy of an ideal gas'               →  (nothing, correctly)
```

For real semantic retrieval, replace `_embed` and `MODEL_TAG` in
`boltzmann_sandbox/indices.py` together — the tag is what stops a consumer from mixing two
representation spaces.

## Proving what you got

Membership is provable in `O(log n)` without holding the rest of the module:

```
prove_block(block_id="sha256:81fb651242d6…", memory_type="procedural")

  { "audit_path": [], "tree_size": 1, "root": "sha256:68c2fc8c4d22…", "verified": true }
```

And the whole brain can be checked — every root recomputed from its blocks, and every block
classified as resolvable, tombstoned or missing:

```
verify_brain()

  { "verified": true, "intact": true,
    "counts": { "resolvable": { "canonical": 1, "semantic": 15, "procedural": 1, "provenance": 17 } } }
```

`resolvable` and `member` are different questions. A block can be a verifiable member of a
version and still not be readable — after a selective install, or a redaction. Membership
proves; resolution reads.

## Publishing and installing

```
push_brain(tag="v1")

  { "manifest":  "sha256:f458abe0a007…",
    "snapshot":  "sha256:5494b10f3269…",
    "reference": "registry-1.docker.io/you/brain:v1" }
```

The manifest digest is the artifact's name, and it is the same name the registry filed it
under. That is what makes `you/brain@sha256:f458abe0a007…` resolvable — the only way to point
at a version nobody can move a tag away from.

### Tags move; digests do not

A tag is a pointer, like a git branch. Pushing to `v1` repeatedly moves it:

```
push 1 -> v1 = sha256:26510403721c
push 2 -> v1 = sha256:ade8f2a1079d
push 3 -> v1 = sha256:2b8f45c1924f
```

Your local history keeps every version (ten by default, `RetentionPolicy.retained_roots`),
each still verifying. **The remote does not.** When `v1` moves, the previous manifest is
untagged, and registries collect untagged manifests.

So the discipline is the same as with container images:

```
v1            moves, "the current one"
v1.0, v1.1    immutable, one per version anyone should be able to return to
```

A push that would overwrite a remote version absent from your history is refused, because the
protocol defines no merge for divergent brains:

```
v1 is at snapshot sha256:0ce547eb89f3, which is not in this brain's history;
the two diverged. Pull and re-commit, or pass force=True to overwrite the remote.
```

### Installing

```
plan_pull(tag="v1")     # one manifest request, no layers

  { "modules": ["canonical", "semantic", "procedural", "provenance"],
    "fetch_layers": ["canonical", "semantic"],   # what you do not already hold
    "reuse_layers": ["procedural", "provenance"],
    "rebuild_indices": ["inverted"],
    "fetch_vector_indices": ["semantic"] }
```

```
pull_brain(tag="v1", memory_types=["semantic"])   # a subset is a first-class outcome
```

A selective install is legitimate, not a partial failure: taking the semantic module without
the canonical one is a real way to consume a brain, and the manifest records what was left
out. What you install carries the digest that was published, and the layers you already hold
are reused by digest rather than transferred again.

`pack_local` does all of the above with no network at all — the brain directory *is* an OCI
artifact, so publishing is a copy.

## Taking knowledge back out

Always plan first. Dropping canonical evidence is privileged: it cascades to everything
derived from it, because a claim whose source was removed can no longer be justified.

```
plan_drop(blocks=["sha256:237765eeac67…"], memory_type="canonical", reason="ingested in error")

  { "privileged": true, "size": 3,
    "dependents": { "semantic": ["sha256:055b77…", "sha256:3e9b23…", "sha256:fc9dee…"] },
    "rederivable": [] }
```

Naming a replacement makes the difference between a deletion and a re-derivation:

```
plan_drop(…, rederive_against="sha256:f5abcb201783…")
  → "rederivable": 3 blocks, against the corrected source
```

Then, and only with `confirm`:

```
drop(blocks=[…], memory_type="canonical", reason="ingested in error", confirm=True)
```

Every removal is recorded in provenance — what was removed, by whom, why. No configuration
turns that off. Older retained roots keep verifying exactly as before.

Two gentler options, which change *accessibility* rather than membership. The block stays in
the composition and keeps proving into the root:

```
supersede_block(block=<newer>, superseded=<older>, memory_type="semantic", reason="corrected")
demote_block(block=<one>, memory_type="episodic", reason="no longer relevant")
```

Demotion is the only option for episodic memory, which is append-only: an episode is a record
of what happened and cannot be rewritten.

Finally, reclaim storage nothing retained needs:

```
prune(dry_run=True)    # always look first; pruning cannot be undone
prune(dry_run=False)
```

Nothing a retained root names is touched, and neither is anything a published tag names —
a layout that published `v1` keeps the manifest and layers `v1` points at.

## Looking at an artifact

Docker Hub classifies a brain as an ARTIFACT and then reports its content type as
*Unrecognized*, because a registry UI can only draw the types it was built to know. Nothing
is broken; use this instead:

```bash
uv run boltzmann-inspect v1          # the published artifact, one manifest request
uv run boltzmann-inspect --local v1  # the local layout
```

```
registry-1.docker.io/you/brain:v1
  manifest       sha256:f458abe0a007…
  artifactType   application/vnd.gaussia.boltzmann.brain.v1+json

  modules (4)
    semantic       15 blocks     2.7 KB
                 layer  sha256:8df714c00ed9…    ← the bytes you transfer
                 root   sha256:bc481741b9ec…    ← the version inside them

  travelling indices (2)
    semantic     vector     17.9 KB
                 built by sandbox-hashing-bow/2

  a full install transfers 51.1 KB
```

Two identities per layer, and the pair is the point. The digest answers "do I already have
these bytes?"; the Merkle root answers "is this the same knowledge?". Two clients that packed
the same blocks with different gzip settings have different layer digests and the same root.

`docker pull` on a brain will fail, correctly — it is not a container image.

## When something goes wrong

| Symptom | Cause | What to do |
|---|---|---|
| `…is at snapshot …, the two diverged` | The remote tag moved, or your brain was recreated | `pull_brain` and re-commit, or push to a different tag. `force=True` only when you mean to replace |
| `travelling index absent for …` | You are in a process that did not build the index | Push from the process that committed, or `pack_local` before it exits |
| `the sources are newer than the installed copy` | The SDK is installed as a built wheel, not an editable | `uv sync --reinstall-package pyboltzmann` |
| `the registry answered 200 with text/html` | `docker.io` is the index host and serves the website | Use `docker.io/...` in `.env`; the sandbox substitutes `registry-1.docker.io` for you |
| `UNAUTHORIZED … Action: push` with valid credentials | Docker Hub's upload challenge advertises `pull` only | Already worked around in the ORAS adapter. If it returns, see `_authorize_write` |
| A push or pull hangs with no output | ORAS invokes the Docker credential helper with no timeout | Already worked around. Credentials must be in the environment; a prior `docker login` does not count |
| `built by …/1 but this client expects …/2` on pull | The artifact's vector index came from another model | Expected: mixing representation spaces would make the ranking meaningless. Pull a matching version, or drop the index from your config |
| Every block matches an unrelated query | You are on an old version | Fixed: function words are dropped before matching |

If none of these fit, `boltzmann-doctor` first and `boltzmann-inspect` second. Between them
they cover the environment, the local layout and the published artifact.
