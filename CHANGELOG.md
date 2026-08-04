# CHANGELOG


## v0.1.0 (2026-08-04)

### Bug Fixes

- **brain**: Never publish a travelling index the brain cannot vouch for
  ([`83bf54d`](https://github.com/gaussia-labs/pyboltzmann/commit/83bf54dff29ced342f93d73b1bc3e070b852eade))

A vector index cannot be regenerated -- that is what rebuildable = False means -- so a brain that
  neither built one nor loaded one holds nothing. Dumping it anyway published a 61-byte layer that
  claimed a vector index, carried none, and still named the model that produced it. The consumer
  loaded it without error, held zero vectors, and had no way to tell. Worse than no layer at all,
  because an absent layer is something plan_pull reports.

Two halves. The brain now records which memory types it built or loaded a travelling index for, and
  _pack_index omits the layer for the rest. And a reopened brain restores what its own layout
  already holds: pull records the manifest it installed, the way pack does, and opening finds the
  manifest whose config digest matches the snapshot and loads the index layers it names. index.json
  was already the only place that knows where a travelling index lives -- nothing was reading it.

The walk over index.json now lives in manifest.py as published_artifacts, which both this and
  pruning need, rather than existing twice.

What remains, and is now visible: the index is persisted when the artifact is materialized, so a
  process that ingests and exits without packing loses it. Brain.travelling_indices says whether a
  push would carry one, and boltzmann-doctor warns when it would not.

- **brain**: Opening a brain is not a request to install anything
  ([`fe90562`](https://github.com/gaussia-labs/pyboltzmann/commit/fe9056209b33ee9212d28d9ba856ff95d6401c5b))

Refusing an index built by another embedding model is right on a pull: the caller asked for that
  artifact, and mixing representation spaces is not what they asked for. Doing it while merely
  *opening* a brain strands it -- every read, every write and every repack goes through opening.

Found by bumping the sandbox's model tag, which made a brain on disk impossible to open. The layer
  is now skipped, travelling_indices reports the module as having no index, and a repack replaces
  it.

- **brain**: Rebuild the structural indices on open and after a pull
  ([`4c1999b`](https://github.com/gaussia-labs/pyboltzmann/commit/4c1999bf59b0306d80f925f09ac722c65dddf8b7))

An index was rebuilt only by the write path, so it was correct in the process that committed into it
  and empty everywhere else. A brain reopened in a new process, or a version installed from a
  registry, held indices describing nothing -- and an empty index does not announce itself: a
  planner consulting it gets no candidates and reports a confident nothing.

plan_pull already listed the structural indices under rebuild_indices. Nothing acted on it, which
  made that field decoration.

Only rebuildable indices are touched. Regenerating a travelling index after a pull would replace
  what a peer published with whatever this client's engine produced, which is the failure §6.3
  exists to prevent, so the write path keeps its own helper: there the blocks are new and this
  client is the only one that can index them.

Unresolvable blocks are skipped rather than read. A block can be a verifiable member of a version
  and still not be readable, after a selective install or a redaction, and an index reads.

Found by searching an installed brain in the sandbox and getting a worse ranking than the
  publisher's for the same query.

- **build**: Anchor the sdist include patterns to the project root
  ([`e391a7a`](https://github.com/gaussia-labs/pyboltzmann/commit/e391a7a8f9ac3be6d96104cbfd0539d11403b372))

Hatchling matches an unanchored pattern by basename at any depth, so `README.md` in the sdist
  include list pulled in every README below the root -- sandbox/README.md ended up inside the
  distribution.

Found by installing the built package instead of the source tree, which is the one thing the test
  suite cannot do from inside it.

- **distribution**: Make the manifest an OCI manifest everywhere
  ([`cffc5ca`](https://github.com/gaussia-labs/pyboltzmann/commit/cffc5ca6420426e4e8d6d73f77133c5970e8174f))

The layout's index.json declared `application/vnd.oci.image.manifest.v1+json` and pointed at a
  document with `artifact_type`, no `schemaVersion` and no `mediaType`. No OCI tool can read that,
  so the README's claim -- the local brain *is* an OCI Image Layout, publishing is a copy and not a
  conversion (§7) -- was false, and `pack`'s promise that the directory becomes an artifact any tool
  can copy was false with it.

The conversion lived in the ORAS adapter, which translated to camelCase on the way out. One brain
  therefore had two documents and two digests, and push returned the local one: pinning by digest,
  the only way to name a version somebody else can move a tag away from, resolved to nothing.

So the OCI shape is now the only shape. Validation accepts the old spelling, because manifests
  written by an earlier version are sitting in real layouts and refusing to read one would strand a
  brain for nothing.

The tag is no longer written into the manifest either. It is a pointer to an artifact, not a
  property of one, and putting it inside gave the same brain a different digest under every tag it
  was published as. A test already asserted the tag must not change the digest; it passed because
  the read path stripped what the write path had added.

push now sends the manifest's own bytes and returns the digest the registry reports, refusing if the
  two disagree. Verified against Docker Hub: the digest agrees on both sides and `repo@sha256:…`
  resolves.

- **distribution**: Read what the registry actually said
  ([`033aa37`](https://github.com/gaussia-labs/pyboltzmann/commit/033aa37775e67a0ab398e029510a0311f6905030))

Two defects, both from the ORAS adapter discarding information the registry had already given it.
  Both found pushing to Docker Hub, and neither is reachable against registry:2, which is why the
  fake-registry tests and the local runs were all green.

**Absence was indistinguishable from refusal.** resolve() reported every failure as one
  DistributionError, and _require_fast_forward treated any DistributionError as "the tag does not
  exist yet, nothing to overwrite". So an expired credential, a 403, or a failing registry all read
  as permission to push -- a safety check that fails open, which is worse than none because it looks
  like one. The status code now decides: ReferenceNotFoundError for a 404, and anything else
  propagates.

Owning the request also fixes the diagnostic. Docker Hub's index host serves the marketing site, so
  `docker.io/v2/…` returns 200 and HTML; oras-py called .json() on it and the error a user saw was a
  JSON parse failure with no hint of where to look. It now names registry-1.docker.io.

**A write asked for a read.** A bearer token is scoped, and ORAS requests exactly the scope the
  Www-Authenticate challenge advertises. Docker Hub's upload endpoint advertises `pull` alone, so
  ORAS got a read-only token, retried, and was refused by the same registry -- whose error named
  `pull` and `push` as required. The credentials were never wrong. The write scope is now requested
  explicitly, taking realm and service from the challenge and replacing only the scope. A registry
  with no challenge is untouched, and a failing token endpoint falls back to the old path.

- **identity**: Pin the timestamp year to four digits
  ([`b6bca6e`](https://github.com/gaussia-labs/pyboltzmann/commit/b6bca6e83c8c81e3cfe63ff11321119571ad2b32))

`utc_timestamp` formatted through `strftime`, whose `%Y` delegates to the platform C library, and
  the two libraries disagree below the year 1000: glibc writes `999-01-01T00:00:00Z`, BSD and macOS
  write `0999-…`. Verified under glibc 2.41.

A timestamp sits inside episodic `occurred_at` and every provenance record's `at`, so it is hashed
  into `block_id`. That made the canonical form depend on the host operating system: two conforming
  clients holding the same instant computed different identities, which is the one divergence this
  module exists to prevent. It also broke the round trip, since `parse_timestamp` refuses the
  unpadded form its own writer produced.

Formatting field by field removes libc from the path entirely.

The explicit padding test is the regression guard, but note that it passes against the old code on
  macOS, where `%Y` happens to pad. What caught this was the Hypothesis property running on a fresh
  example database in CI, on Linux.

- **indices**: Export TravellingIndex from the package root
  ([`253a00a`](https://github.com/gaussia-labs/pyboltzmann/commit/253a00a421f5834de77758292bfe497ef12c8cdb))

The README tells a reader to import it from `boltzmann.indices`, and that raised ImportError -- an
  index reporting `rebuildable = False` must satisfy this protocol, so it is public surface, not an
  internal detail. The other three names in the module were already exported.

- **query**: Drop function words before matching in the scan
  ([`43246b3`](https://github.com/gaussia-labs/pyboltzmann/commit/43246b346cf53bdf7faa72aece1e43392a976bfc))

The scan counted every whitespace-separated word as a query term, so a block matched if any of them
  appeared in its text. Asking a brain about thermodynamics -- a subject it knew nothing about --
  returned all fifteen of its blocks, each with a score, because "an" was present in fourteen of
  them and "of" in seven. A filter that admits everything is not a filter.

Removing them also fixes the ranking, not just the filtering: the denominator stops rewarding a
  block for sharing grammar. The query that had put the wrong block first now puts "privileged
  cascade" first at 1.00, where before it did not reach the top two.

The list is grammatical rather than frequency-based. A frequency list eventually swallows a term
  some brain treats as knowledge, and a stopword too many is an answer nobody can find. A query that
  is nothing but function words keeps them, because answering "what is it" with nothing found is
  worse than answering it badly.

- **retention**: Count the published tags as roots when pruning
  ([`c03644d`](https://github.com/gaussia-labs/pyboltzmann/commit/c03644de427df7aa47ee156c15c8570352eb01d0))

A layout has two kinds of root and only one was honoured. Snapshots name knowledge; they do not name
  the artifact built from it -- the manifest and the packed layer per module -- and that is
  precisely what a tag names.

So packing a tag and then pruning reclaimed the manifest and its layers, because no snapshot
  mentioned them, which was true and beside the point. It left index.json pointing at bytes that
  were gone: a layout claiming a tag it could no longer serve, unreadable by any OCI tool and
  unreopenable by this SDK.

Only what the tags name now is kept, so republishing a tag still lets its previous manifest go. A
  store with no layout index prunes exactly as before.

Found by validating the local layout after a demo run and getting FileNotFoundError on the manifest
  index.json had just named.

- **sandbox**: Keep ORAS away from the Docker credential store
  ([`51b9843`](https://github.com/gaussia-labs/pyboltzmann/commit/51b9843a9cc22563c60be45b1b5e74f4e0bd90e3))

Before every request ORAS resolves credentials, and when ~/.docker/config.json names a credsStore it
  shells out to the helper -- docker-credential-desktop on a Mac. That subprocess.run carries no
  timeout, so a helper that blocks blocks the whole run: the symptom is a push that never returns,
  with no output and no error.

Pre-seeding an empty credential set stops the lookup, since ORAS loads the config once and only when
  it has none. Nothing is lost: credentials here are explicit by design, and an explicit login sets
  them by another path.

The consequence to know about is that a prior `docker login` does not authenticate this sandbox.
  State the token in the environment.

- **sandbox**: Resolve configuration where it lives, and address Docker Hub
  ([`09eaea0`](https://github.com/gaussia-labs/pyboltzmann/commit/09eaea0473e00dd04e0ed84bc46a3a8542b7c267))

Three things that all bite the moment an MCP client launches the server, because it does so with a
  working directory of its own choosing.

`docker.io` is Docker Hub's *index* host, not its registry API. A request to
  `https://docker.io/v2/…` reaches the website and returns 200 with HTML. `docker pull
  docker.io/user/repo` works only because the Docker CLI substitutes the endpoint for you; a library
  that does not is surprising rather than wrong, so the substitution happens here and the doctor
  shows both spellings.

`.env` was discovered by walking the call stack, which finds nothing when the caller is `python -`
  and finds the wrong directory when the caller is an MCP client. It is now looked for beside the
  project, with a local one still winning.

A relative BOLTZMANN_BRAIN_PATH resolved against whoever started the process, so `./brain` named a
  different directory for every caller. A relative path in a config file now means relative to that
  file.

- **sandbox**: Round the vectors when built, not only when dumped
  ([`cc630e3`](https://github.com/gaussia-labs/pyboltzmann/commit/cc630e35b0c5aeddcc2879770cb1a15c809f7a09))

Rounding on the way out left the publisher ranking with full precision and a consumer that loaded
  the index ranking with six decimals. The dumps matched, so the layer digest was reproducible and
  the difference was invisible -- until a near-tie, where the two ends would order the same results
  differently while both claiming to hold the same index.

Rounding at build time makes "the same index" mean the same numbers. The cost is that a vector is
  unit length only to within the rounding, so a self-match can score 1.000001; the test states the
  bound and why.

- **sandbox**: Stem tokens so a word matches its own inflections
  ([`50a4569`](https://github.com/gaussia-labs/pyboltzmann/commit/50a45692a0111930947f0fd707f03e8a1fba2388))

remove and removing landed in different posting lists and different hash buckets, so neither index
  credited the block that answers a question about removing something. Only the SDK's substring scan
  caught it, and with one vote of three it lost to a block matching on grammar.

Suffix stripping with a length floor, not Porter: `was` does not become `be` and `indices` does not
  become `index`, and the tests say so rather than implying a rule that half works. The trailing `e`
  is what closes the family -- English drops it before -ing and -es, so without that rule a verb
  never matches itself.

The model tag goes to /2. Nothing about the arithmetic changed, but what gets hashed did, so a /1
  vector sits elsewhere in the same 256 dimensions -- which is exactly what the tag exists to
  refuse.

- **sandbox**: Stop the demo from deleting the brain it does not own
  ([`72f3731`](https://github.com/gaussia-labs/pyboltzmann/commit/72f37317c8c4b0b19095ba34bb149309faea75c2))

It wiped BOLTZMANN_BRAIN_PATH on every run, so ingesting real knowledge and then running the demo
  destroyed it. It now uses two directories of its own and says so at the end.

It publishes to its own tag too, and forces it. A brain created empty on every run always diverges
  from what the previous run published, which is exactly what the fast-forward guard exists to
  refuse -- so the demo owns a tag rather than borrowing the configured one and forcing over a
  version somebody meant to keep.

And it no longer blames the registry for the SDK's own refusals: a divergence, a missing repository
  and a rejected artifact now read as three different findings, because only one of them says
  anything about OCI artifact support.

### Build System

- Keep releases in 0.x until 1.0.0 is deliberate
  ([`1903f68`](https://github.com/gaussia-labs/pyboltzmann/commit/1903f688048716200e3d8cc4e2be66ce70e8116b))

semantic-release defaults `major_on_zero` to true, so the first `feat` in the history read 0.1.0 as
  "time for 1.0.0" and would have published an API-stability promise nobody made -- verified with
  `semantic-release --noop version`, which printed 1.0.0 before this change and v0.1.0 after it.

It also contradicted `major_version_zero` under [tool.commitizen], which already said to stay in
  0.x. The two tools read the same history and disagreed about what it meant.

- Leave documentation to its author, not to the formatter
  ([`ab471a9`](https://github.com/gaussia-labs/pyboltzmann/commit/ab471a99be56a9260d89e180462d4747db5088ff))

Ruff 0.16 formats Python blocks inside Markdown. The README uses aligned trailing comments so three
  consecutive calls read as a table; the formatter collapses that alignment, and the pre-commit hook
  does not catch it because it filters to Python files -- so `ruff format .` and the hook disagreed
  on the same tree.

Excluding Markdown makes them agree again, and keeps the choice where it belongs.

- Pin the pre-commit ruff to the dev dependency's version
  ([`720166e`](https://github.com/gaussia-labs/pyboltzmann/commit/720166e817598a40afc8d4a6827565f5725b914c))

The hook was on v0.8.6 while the dev group had 0.16, and the two disagreed on rules: during the
  initial import the hook caught a PT019 that `uv run ruff check` reported clean. That direction is
  the safe one, but the drift cuts both ways and the next one might not.

Also moves off the deprecated `ruff` hook id to `ruff-check`, which is what the alias resolves to,
  and leaves a comment saying why the two versions have to move together.

- Publish as pyboltzmann, and keep only the markers this suite uses
  ([`38800b4`](https://github.com/gaussia-labs/pyboltzmann/commit/38800b4bc2ae890cb4e25b6c30d10e8405d11592))

`boltzmann` on PyPI belongs to an unrelated 0.0.1, so the first `uv publish` would have failed with
  403 -- and the README told people to install something that was not this. The distribution is now
  `pyboltzmann` and the import package stays `boltzmann`, the same split as `pygaussia` providing
  `gaussia`. The two names are separate constants in the doctor, because asking the metadata for the
  import name returns nothing and looking for the sources under the distribution name finds nothing.

`requires_gpu` and `requires_api_key` came from a sibling project and mean nothing here: the SDK
  embeds no model and calls no service, which is Principle 5. With --strict-markers, an unused
  marker is a name that looks available and is not.

### Chores

- Lock jsonschema for the schema tests
  ([`70a5cb0`](https://github.com/gaussia-labs/pyboltzmann/commit/70a5cb0a8ed0c2362b25c0730c34611a9291b020))

The emitted JSON Schema is only worth emitting if it validates real documents, so the tests need a
  real validator. Dev-only: the runtime still depends on nothing but pydantic and rfc8785.

- Scaffold the boltzmann package with uv
  ([`646348c`](https://github.com/gaussia-labs/pyboltzmann/commit/646348c3dc59905af91ae186d533431bcc0dfe3c))

Conventions follow the sibling pygaussia repo so the two are navigable by the same habits: src
  layout on hatchling, ruff at 120 columns with the same rule set, strict mypy, pytest with
  coverage, and commitizen plus semantic-release driving the version off the commit history.

Two deliberate departures. requires-python and the ruff target agree at 3.11 rather than drifting
  apart, and flake8-type-checking is told that pydantic resolves annotations at runtime, without
  which every model field would be moved into a TYPE_CHECKING block and stop validating.

- **tooling**: Add a commit skill for this repo
  ([`0e5a8db`](https://github.com/gaussia-labs/pyboltzmann/commit/0e5a8db474f4e3541f4e399d1c24218728bc02fd))

The version and the changelog are derived from the commit history, so a message that does not parse
  silently drops out of the changelog and never bumps a version. The skill states the format, points
  at the parser options in pyproject that decide what a type does, and gives grouping guidance.

It forbids the Co-authored-by trailer outright.

### Continuous Integration

- Publish to pypi from master and develop
  ([`ee0a025`](https://github.com/gaussia-labs/pyboltzmann/commit/ee0a025835fc237a31539abc92baa9a45202d2e8))

Mirrors the release pipeline in pygaussia: test, then semantic-release, then PyPI, then a GitHub
  release, with the publish steps gated on whether a version was actually cut.

The test job runs the matrix the package claims in requires-python and its classifiers rather than
  one interpreter. A wheel that says it supports 3.11 and is only ever tested on 3.13 is an untested
  claim, and this one is about to be public. All three pass today. The release job builds on 3.11,
  which is the repo's .python-version and the target ruff and mypy are configured for.

Needs PYPI_API_TOKEN. The project does not exist on PyPI yet, so the first upload needs an
  account-scoped token; it can be narrowed to the project afterwards.

- Publish to pypi through trusted publishing
  ([`8d78979`](https://github.com/gaussia-labs/pyboltzmann/commit/8d7897952533f1fa0a004bb12c23d4766692d7fe))

PyPI verifies the OIDC token the job already mints against the publisher registered for the project,
  so the step needs no credentials. Drops the PYPI_API_TOKEN secret entirely.

It also removes the awkward step this project would otherwise have needed: pyboltzmann does not
  exist on PyPI yet, and a token scoped to one project cannot be minted for a project that is not
  there, so the first upload would have required an account-wide token in a repository secret.

- Sync docs to the central repo on push to master
  ([`8241bf1`](https://github.com/gaussia-labs/pyboltzmann/commit/8241bf16673e0de92a9c538f0cd6e704f22fd682))

Mirrors the workflow in pygaussia: copy the mdx into the target_dir named by docs/docs-sync.json and
  open a pull request against gaussia-labs/docs, so the SDK owns its pages and the central repo
  reviews them.

The pull request body carries the one thing the automation cannot do. The workflow copies mdx only
  and never rewrites the central docs.json, so a page this sync *adds* ships unreachable until its
  path is added to the Boltzmann SDK tab by hand.

Needs DOCS_REPO_PAT, with write access to the docs repo and permission to open pull requests.

### Documentation

- Add the SDK documentation source
  ([`5d65c25`](https://github.com/gaussia-labs/pyboltzmann/commit/5d65c259613373ff49627a16575fd4917f2ebcaa))

Thirteen pages: getting started, the five concepts a reader needs before the API makes sense
  (architecture, memory types, identity, merkle, interfaces), and a guide per protocol contract.

Every snippet was executed against the SDK before it was written down, and the
  property-versus-method spelling of all 85 documented attributes was checked by introspection --
  which caught five pages calling a property.

Links use the path a page has once published under sdks/boltzmann, not the path it has in `mint
  dev`. A link that works in the preview and 404s in production is the worse trade; docs/README.md
  records that, and what the sync workflow does and does not carry.

- Rewrite the README for a client that works
  ([`729744d`](https://github.com/gaussia-labs/pyboltzmann/commit/729744dd32c809e6db63a0e5f0d2ea2031a76ddb))

It described a package of interfaces whose operations raised NotImplementedError, and its usage
  example built modules and snapshots by hand -- one call of which no longer exists. That is worse
  than no README: a reader would have followed it and found an API that had moved.

Now it shows what the SDK actually is: open a directory, call methods, they work against an OCI
  artifact. The whole lifecycle of Section 11, then removing knowledge with the cascade, then
  publishing and installing. Both code blocks were executed and their assertions hold, so they
  cannot describe an API that drifted.

The two tables state the line the SDK draws in one place: it does whatever the protocol defines
  mechanically, and the implementer supplies whatever the paper assigns elsewhere -- the model, the
  ranking, the index engines, the MCP layer.

- Trim the readme to a landing page
  ([`f378400`](https://github.com/gaussia-labs/pyboltzmann/commit/f378400104ff0b5d814bc9eff89abdeaf60a1685))

The decisions the SDK closes, the invariants it makes structural, the plug points, the conformance
  recipes and the retention and distribution walkthroughs now each have a page under docs/, so
  keeping a second copy here means two things to update and one of them going stale.

What stays is what a reader needs before they have decided to read anything: what this is, the line
  it draws, how to install it, one lifecycle example, and where to go next.

Doc links are absolute because this file is also the PyPI long description, where a relative link
  resolves against pypi.org and 404s.

- **sandbox**: Add a walkthrough of using a brain
  ([`d3b3c17`](https://github.com/gaussia-labs/pyboltzmann/commit/d3b3c17cdca1a2d2cae65d79380f78e7126f68f1))

The README explained what the sandbox is and how to configure it, and left the actual work
  undocumented: what you type to put knowledge in, get it out, prove it, publish it, install it
  elsewhere, and take it back out.

Written from the runs in this repository rather than from the interfaces, so the outputs are real --
  including the rejections, which is the half a reference would skip. A rejection is information
  here: a candidate that cites evidence it was not derived from, or duplicates a block already
  present, comes back with a code rather than raised as an error, and re-submitting a set already
  committed rejects all of it.

Ends with a table of the symptoms this repository actually produced and what each one means, because
  every trap in it cost an afternoon to diagnose the first time: a diverged tag, a travelling index
  lost between processes, a stale non-editable install, docker.io serving a website, a write asking
  for a read scope, and a credential helper with no timeout.

- **sandbox**: Document the credentials, the findings and what is untested
  ([`a7dc58a`](https://github.com/gaussia-labs/pyboltzmann/commit/a7dc58a9eb0a3320f45024e6f7a505b12c384510))

Says what the local run proved, step by step, and says plainly that Docker Hub is not among it: the
  question of whether a hosted registry accepts our artifactType and config media type stays open
  until someone runs it with real credentials, and a rejection is the finding rather than something
  to hide.

Records the four defects the sandbox has already caught, and the one limitation it exposed without
  fixing -- a reopened brain has an empty travelling index, because only the manifest names index
  layers and pull does not write it into the layout the way pack does. Bounded in effect, worth
  deciding on rather than drifting into.

- **sandbox**: Record that Docker Hub accepts the artifact
  ([`fb91cd3`](https://github.com/gaussia-labs/pyboltzmann/commit/fb91cd36c3f99613401c25ee249267ae74ca2193))

The question this sandbox was built to answer, answered: a manifest whose artifactType and config
  media type are the protocol's own is accepted with 201 Created, and a brain round-trips through
  Docker Hub with its digest and its travelling index intact.

Three client fixes were needed to get there and none of them touched the protocol. Two are written
  down for whoever builds the next Boltzmann client over oras-py, because a hosted registry is the
  only place they surface: docker.io is not the registry, and the upload endpoint's challenge asks
  for less scope than the upload needs.

### Features

- Expose the public surface and document the boundary
  ([`2e9bb10`](https://github.com/gaussia-labs/pyboltzmann/commit/2e9bb1016ec57c28b3ef630cc18681c0dc146b4a))

One import for the things a caller needs, and a README that says what the SDK does and does not do
  rather than only how to call it. The line it draws is the one worth writing down: implemented is
  whatever every conforming client must compute identically, declared is whatever the paper leaves
  to the implementation.

Which is why there are no NotImplementedError stubs anywhere, and a test enforces it. An
  unimplemented function is worse than an interface: it looks callable and is not.

The README's usage example is executable and was run, so it cannot drift into describing an API that
  no longer exists.

- Give every declared surface an implementation
  ([`d1aebc4`](https://github.com/gaussia-labs/pyboltzmann/commit/d1aebc45b1c48e42343e893fa8b17bd33edd0b11))

Four things were declared and unreachable. A type nobody constructs and an enum member nobody
  produces each promise a capability that does not exist, and a reader cannot tell the promise from
  the feature.

**plan_pull** produces InstallPlan, which nothing did. Resolving a manifest is cheap and downloading
  it implies downloading nothing else, so the cost of an install can be known before paying it --
  and over an existing brain the plan reports only what actually moved, which is the incremental
  update made visible.

**define_rederivation** produces TaskOperation.REDERIVE. Section 8.1 is explicit that re-derivation
  runs only when a replacement has been registered, so it is a distinct operation rather than a
  flag: a block's citation is part of its identity, so one citing excluded evidence cannot be
  repaired in place, only replaced by a new block citing the new source. The task names what it
  replaces, so the resulting provenance says what the run was for.

**PENDING_REVIEW** is now reachable. The three verdicts are not a severity scale: a malformed
  proposal can never be committed, a contradiction is well-formed and disagrees with what is held,
  and a check may decline to decide, which is not deciding against. None of the protocol's own
  checks decline -- declining is a deployment's prerogative, for a claim needing a subject-matter
  expert or a licence question needing a lawyer -- so UndecidedValidator ships outside the defaults
  as the shape such a check takes. A real defect alongside a declined check still rejects, or
  declining would launder malformed input into review.

A CONTRADICTED verdict now names the blocks it conflicts with. Saying something disagrees without
  saying with what is not enough for a reviewer to decide.

The two schema constants that were never read are the $id of the task and evidence schemas.

crypto_shred and lineage_rewrite stay out of v1 deliberately: one needs encryption at rest and the
  other invalidates prior roots for every consumer. The policy refuses them by name rather than a
  stub pretending to work.

- **blocks**: Add the five typed memory blocks
  ([`2b4b52f`](https://github.com/gaussia-labs/pyboltzmann/commit/2b4b52f8b9eaaa9785996b3cfcf613d414d0328c))

Canonical, episodic, semantic, procedural and provenance, each with the schema the paper leaves
  open. What gets hashed is the envelope rather than the payload alone, so the memory type and the
  schema version are bound into the identity: two blocks with identical payloads and different types
  are different blocks.

An absent optional field is dropped rather than serialized as null, so {"a": 1} and {"a": 1, "b":
  null} are one block. Decoding refuses bytes that are not already canonical instead of normalizing
  them, because normalized bytes would hash to an identity different from the one they were filed
  under.

The canonical block departs from Section 5 of the paper, which lists a registering actor, a
  timestamp and a supersedes link among its fields. That contradicts Section 8.1, which requires
  re-registering an identical blob to be a no-op: with actor and timestamp inside the hash, two
  people ingesting the same PDF would obtain different blocks and deduplication would never fire.
  The block is therefore a pure statement about observed bytes, and everything actor-dependent moves
  to provenance. The paper is corrected to match.

Observed bytes are addressed by OciDigest, not BlockId. A source is a transportable file; the block
  is the knowledge-level statement about it. Read with media_type and size, a canonical block is an
  OCI descriptor over the evidence, which is why publishing a brain is a copy and not a conversion.

- **brain**: Implement the client over an OCI layout
  ([`1b06a3e`](https://github.com/gaussia-labs/pyboltzmann/commit/1b06a3e3f4794cdb019a8e6db7125c4ccfda0542))

The class you instantiate: open a directory, call register, ingest, commit, search, pack, push,
  pull, and they work against the layout. What it delegates is the two things the paper assigns
  elsewhere -- what knowledge a source yields, through CandidateProposer, and how to rank, through
  QueryPlanner. Neither ships.

A commit is atomic in the way that matters. Content-addressed blobs go in first and the snapshot
  pointer moves last, so a failure part-way through leaves orphan blobs a prune reclaims and the
  previous snapshot still current. There is no state in which a root names a block the store does
  not hold. Every mutation funnels through one private write path, which is what keeps the Section
  7.1 design rule structural.

One commit is one version however many modules it advanced, because adding a semantic block also
  advances provenance and those are not two versions of the brain. A brain's first version has no
  parent: the empty snapshot a fresh handle starts from is a placeholder, and chaining to it would
  leave an unresolvable digest in every ancestry.

Pushing refuses to overwrite a remote whose snapshot is absent from the local history. The paper
  defines no merge for divergent brains and content addressing does not help -- the blobs would
  survive while no retained root named them -- so the safe behavior is to refuse and say where the
  two parted. A full pull adopts the remote snapshot document verbatim rather than rebuilding an
  equivalent one, because a fresh created_at would change the digest and make a push back to the
  same tag look like a divergence when nothing diverged.

Republishing a partial install over the tag it came from is refused, since the modules never fetched
  would silently disappear. Publishing it elsewhere is allowed: a semantic-only brain is a
  legitimate artifact.

- **distribution**: Add OCI media types, manifests and layer packing
  ([`6fc61c0`](https://github.com/gaussia-labs/pyboltzmann/commit/6fc61c0f4bae087faa5cd6b6819bca7f2c04b402))

The seam between OCI and Boltzmann, fixed here rather than left open: two clients that disagree on
  the artifact type cannot pull each other's brains. A descriptor carries both identities the
  manifest needs -- the digest names the file, and an annotation names the internal Merkle root of
  the composition inside it. Two registries holding the same brain agree on digests while knowing
  nothing about modules or snapshots, and that annotation is what closes the gap Section 4.3
  describes.

One layer per module is a necessary condition, not an optimization: if everything were one file,
  selective installation would mean downloading it all. A canonical layer carries the observed bytes
  and not only the blocks that describe them, or it would arrive as claims about evidence the
  consumer cannot read.

Packing is deterministic, with tar and gzip timestamps, ownership and mode all pinned. A layer is
  content-addressed, so two clients packing the same composition must produce the same digest --
  without that, push deduplication stops working silently and every push re-uploads everything. gzip
  rather than zstd because it is in the standard library, and needing a compression dependency to
  read a published brain would trade portability for a few percent.

Two transports satisfy one interface. The ORAS client talks to a registry and uploads paths that
  already exist in blobs/, so a push transfers files rather than serializing them. The layout
  registry moves brains between OCI layouts, which is a first-class transport target and the reason
  the whole path is testable offline.

The ORAS tests run against a fake registry. They pin the wire shape and the digest verification;
  they do not prove the client works against a real registry, and the test module says so.

- **distribution**: Make the vector index travel, and allow publishing a subset
  ([`1e28e57`](https://github.com/gaussia-labs/pyboltzmann/commit/1e28e57566eb5d6366cfc883d39fd6675ce83d39))

All the plumbing for a travelling index existed -- the model tag on ModuleRef, the media type,
  is_vector_index, vector_index_for -- but pack never created the layer and pull never fetched it.
  The one derived structure Section 6.3 says has to travel, because rebuilding it needs an embedding
  model a model-agnostic client does not carry, did not travel. Plumbing that suggests a capability
  nobody implemented is worse than no plumbing.

What was missing from the interface was serialization: an Index could be built and searched but not
  turned into bytes. TravellingIndex adds dump and load, and an index that reports rebuildable=False
  must satisfy it -- a module layer can only carry bytes, so an index no client can rebuild and
  nobody can publish would arrive missing with nothing able to regenerate it. Packing one that
  cannot dump now fails loudly instead of silently omitting it.

An index built by a different embedding model is refused on pull rather than loaded. Vectors from
  two models occupy different representation spaces, so mixing them would produce rankings that mean
  nothing -- and the model annotation exists precisely so a consumer can tell before it is too late.

**pack(modules=...) publishes a subset**, because a brain's sources can be gigabytes while its
  derived knowledge is kilobytes, and the right to derive from a book is not the right to
  redistribute the book. But canonical cannot be omitted when a derived module is included: R1 makes
  canonical evidence the root of re-derivation, and an artifact whose citations point nowhere could
  be trusted and neither audited nor re-derived, which is what Section 4.2 says is lost without it.
  Canonical or episodic alone is fine -- neither cites anything.

A subset publishes a projection of the snapshot, not the snapshot, so the config describes what the
  artifact actually carries. A projection is in nobody's history, so the manifest records the full
  snapshot it came from and the fast-forward check compares against that -- otherwise pushing the
  same projection twice would look like a divergence.

- **identity**: Close the protocol's open decisions on identity
  ([`c3d40e1`](https://github.com/gaussia-labs/pyboltzmann/commit/c3d40e17a52ca55681dfe6b23b332f45f9d8f46c))

The paper leaves the deterministic serialization behind block_id open (Section 12), but an SDK
  cannot: two clients that disagree on it do not share a brain at all. It is fixed as JCS (RFC
  8785), tagged "jcs/1" in every envelope so a future serialization can coexist rather than replace
  it. JCS over a binary encoding because a block is a small record and the protocol targets several
  languages, where a canonical form a human can read and grep is worth more than compactness.

Floats and integers outside the IEEE-754 safe range are refused inside a payload. JCS defines float
  serialization through ECMAScript rules that are hard to reproduce identically across languages,
  and an unsafe integer loses precision in any double-backed parser. Either divergence would mean
  two conforming clients computing different identities for the same knowledge.

The three levels of hashes of Section 6.4 are three types. BlockId, MerkleRoot and OciDigest share
  an algorithm and never a meaning, so none of them is a str and none is interchangeable: mypy
  rejects the confusion, and untyped data reaching runtime raises DigestKindError.

Timestamps are RFC 3339 in UTC with second precision and nothing else, because isoformat offers
  several spellings of the same instant and a timestamp inside a payload is part of an identity.

- **ingest**: Add the ingestion contract and the validation gate
  ([`58ff43f`](https://github.com/gaussia-labs/pyboltzmann/commit/58ff43f3e2bc34ce363c31dcdd55ad64e75c7b71))

The boundary with the external model, as types. A Candidate is deliberately not a Block: it has no
  block_id, because an unvalidated proposal has no identity, and it carries a raw payload rather
  than a typed model because it has not been checked. There is simply no method on the proposer
  interface that could reach a Merkle DAG.

A ProcessingTask refuses to invite proposals for canonical or provenance memory. Canonical
  registration is deterministic and needs no interpretation, and the ledger is written by the
  protocol; leaving either open to a model would put it in charge of evidence or of the audit
  record.

The gate implements the checks Section 8.3 assigns to the protocol -- allowed type, schema, evidence
  installed, duplicates, dangling relations, basic contradictions -- because they are mechanical:
  they judge shape, never content. Whether knowledge is good is the model's business, and a
  deployment that wants domain checks adds its own Validator.

A contradiction yields CONTRADICTED rather than REJECTED. It is information, and what to do with it
  is a policy decision rather than a defect.

A candidate's citations are written onto the block when its schema has an evidence field. A block
  has to be self-describing: a consumer who installed only the semantic module has no ledger to
  consult, so a citation living only in provenance would leave them holding knowledge with no way to
  see what it rests on.

Normalization pipelines are registered by name and version, because a normalized view is only
  evidence if the transform that produced it can be reproduced.

- **ingest**: Emit the JSON Schema behind boltzmann.candidates/v1
  ([`b770bd5`](https://github.com/gaussia-labs/pyboltzmann/commit/b770bd5002e7fae32fcf66c2052160aec7cafdf0))

A ProcessingTask told the model its answer had to satisfy "boltzmann.candidates/v1" and gave it a
  name, not a schema. The payload is the part the model most needs to get right and the part it had
  least help with: Candidate.payload is dict[str, Any] in Python, so pydantic's own schema says "any
  object" and offers no hint that a semantic block needs kind, label and statement, or that kind is
  one of five values.

The SDK already knew all of it, because the block classes are the schema. This composes it: one
  candidate variant per memory type, each pinning memory_type to a constant and replacing the opaque
  payload with that type's block schema, joined by oneOf. Restricting to a task narrows the
  variants, so a model constrained by the schema cannot even express a proposal the gate would
  reject on shape.

It is generated from the same classes the gate validates against rather than written alongside them,
  so the two cannot drift apart.

The tests validate real documents through a real Draft 2020-12 validator, not the shape of the
  schema document, because a schema that is well-formed and rejects valid input would pass the
  weaker test. Schema-valid is asserted to mean gate-valid for every proposable type.

Also resolves the two schema constants that had been declared and never used: they are now the $id
  of the task and evidence schemas.

- **merkle**: Version compositions with an RFC 6962 Merkle DAG
  ([`5c720a4`](https://github.com/gaussia-labs/pyboltzmann/commit/5c720a4a5a1b7fb5ad731994e111e35e5b54b4a7))

Leaves are the composition's block ids sorted lexicographically, which is what makes the root a
  function of the set rather than of insertion order. Section 6.2 claims that two parties who
  assembled the same blocks obtain the same root; a layout that preserved insertion order would not
  deliver it.

RFC 6962 over a naive binary tree because splitting at the largest power of two below n is
  unambiguous, where duplicating the last node on an odd level admits a second-preimage attack
  (CVE-2012-2459). Leaves and internal nodes are prefixed differently, so a leaf hash can never pass
  for a node hash.

Internal nodes are derived rather than stored, so the artifact persisted per snapshot is the sorted
  leaf list and the root. That is also why differencing two versions is a set operation over leaf
  lists instead of a descent through stored nodes: what an incremental update needs is which blocks
  to fetch, not the shape of the walk that computed it.

The construction sits behind a MerkleLayout interface. With sorted leaves the blocks are shared
  across versions -- which is what matters for transfer, since they are the bytes -- but the
  internal spine is recomputed. A prolly tree would share the spine literally; the paper is
  corrected to claim only what any conforming layout delivers.

Roots are cross-checked against hashing computed by hand for sizes 0 through 8, so the builder and
  the verifier cannot be wrong in the same way.

- **module**: Add a shared ledger view over provenance
  ([`beda574`](https://github.com/gaussia-labs/pyboltzmann/commit/beda57490a85b2bbf885398acff05d8171182a3a))

Both paths need to read the ledger: a query has to know what a newer block superseded, and a drop
  has to know what cited the evidence it is about to exclude. Reading it means decoding every
  provenance block, so it is built once and passed around rather than re-walked per question -- and
  it lives in the module layer because both callers already depend on modules and neither should
  have to depend on the other.

The two reverse indices are the point of it. The ledger records which evidence a block cites; a
  cascade needs the opposite direction, so dependents inverts derived_from and superseded_by inverts
  supersession.

Adds DemotionRecord, which is what makes demotion implementable without inventing new storage.
  Recording accessibility in the ledger rather than in a field on the block is not a shortcut: a
  block is immutable, so if accessibility lived on it, demoting a block would change its block_id
  and make it a different block. What the record deliberately does not carry is a score -- the paper
  leaves the decay function open, so how much a demoted block is penalized stays a retrieval
  strategy.

The scan now holds back demoted blocks alongside superseded ones, since Section 10.4 treats both as
  accessibility rather than membership.

- **module**: Add compositions, snapshots and the index interface
  ([`b4518aa`](https://github.com/gaussia-labs/pyboltzmann/commit/b4518aab7d82e0689826f54992ed69aff0206825))

A composition is the set of blocks that form one version. It is the object every removal operates
  on: a drop does not mutate a block, it derives a new composition and therefore a new root.

The composition is persisted, not only computed. A root can be verified but not inverted back into
  the set it commits to, so a snapshot naming only roots would identify versions it could not
  reopen. The document that carries the leaf list is exactly what a module layer ships when the
  brain is published, so ModuleRef names it by digest and Module.persist writes it.

Reading a block goes through membership before it goes through the store. A block that exists in the
  store but in no installed composition was dropped, or belongs to a module this client did not
  install; either way no installed root commits to it, and returning it would break the guarantee
  that every result is verified against the snapshot.

Module is read-and-derive only, with no write method at all. That is what makes "the LLM never
  writes directly to the Merkle DAGs or to the indices" (Section 7.1) a property of the code rather
  than a rule to remember.

The index layer is the interface and the six kinds the paper names. No engine ships: which engine
  backs an index is explicitly the implementation's choice (Section 6.3), and rebuildable says which
  of them a client can regenerate without a model.

- **protocol**: Declare the surface as four composable contracts
  ([`bee1c67`](https://github.com/gaussia-labs/pyboltzmann/commit/bee1c6759bb6cd536c326312c021c83178de2ba6))

BrainReader, BrainWriter, BrainRetention and BrainDistribution, with BoltzmannProtocol composing all
  four. The split is because read and extend are separable and most consumers only read: a client
  that satisfies BrainReader is conforming for what it claims, and does not have to pretend to
  support writes it will refuse. Every contract is runtime_checkable, so an implementer can assert
  conformance instead of hoping for it.

Distribution is named pack, push and pull rather than publish and install. The operations are the
  ones Section 7 enumerates, but their shape is the one Section 7.3 describes -- a brain moving
  between a remote artifact and a local layout in both directions -- and that is the vocabulary
  everyone already has from version control and container registries. "Install" would suggest
  something executable is being set up.

The eight invariants the paper states normatively are tested as executable claims, so a violation is
  a failure rather than a matter of remembering: a candidate is not a block, a query field never
  names an index, the bundle has no answer field, the episodic module refuses to drop under any
  policy, and auditability cannot be configured away.

- **query**: Add the declarative query, the evidence bundle and a scan
  ([`0b6c142`](https://github.com/gaussia-labs/pyboltzmann/commit/0b6c14266e45af2da746018ecbbcde075213a742))

A Query names no index anywhere, which is Principle 7: the caller expresses intent and choosing
  indices is the implementation's job. RetrievalMode names strategies, not engines, so asking for
  lexical matching does not demand an inverted index. A query with no terms at all is valid, because
  "the episodes of last May" is a complete request and refusing it would make recency and subject
  filters unusable on their own.

EvidenceBundle has no field for an answer. Not omitted for brevity -- absent by design, because the
  brain returns data and the consumer decides how to phrase it (Section 9.3). Scores are strings for
  the same reason payloads forbid floats: a number whose textual form varies across languages does
  not belong in a wire format. The bundle carries the roots it verified against, so verified: true
  is a checkable claim rather than one to be trusted.

Search has to work on a brain that was just opened, and no index engine ships. The scan therefore
  does the part that belongs to the protocol -- filter, resolve, verify, report provenance -- and
  says plainly that it is not a ranking strategy: matching is a term scan, traversal is linear, and
  the score is term coverage rather than relevance. An implementation that wants relevance injects a
  QueryPlanner and replaces candidate generation; verification stays where it is either way.

Two things the scan gets without an engine, because the protocol stores them symbolically on the
  block. Relations live on semantic blocks, so associative expansion is a pure function of the
  composition. And the ledger says what a newer block replaced, so a superseded block is held back
  unless asked for -- which is what Section 10.4 means by supersession changing accessibility rather
  than membership.

- **retention**: Add the removal types and the retention policy
  ([`97f4278`](https://github.com/gaussia-labs/pyboltzmann/commit/97f4278ff6a0cc6548b30d01d8df10b8ebaf5129))

The four mechanisms of Section 10 are four distinct types, because conflating them is the mistake
  Section 10.1 warns about. Drop excludes a block from a composition and is the cleanup path.
  Supersession and demotion change accessibility, not membership. Pruning reclaims what no retained
  root needs and never decides what to forget. Redaction destroys bytes a retained root still names,
  and is for law and safety rather than for cleanup.

Policy is configuration, as Section 10.7 says, so who may drop from where and how deep a cascade
  runs before review are deployment decisions. What is not configurable is auditability:
  record_removals is a property that is always true and never a field, so no settings file and no
  deserialized document can turn it off.

Two refusals are baked in rather than left to a policy author. Dropping from the episodic module
  raises whatever the policy says, because append-only is a property of the protocol. And canonical
  drops and redaction are both off by default, because excluding evidence forfeits re-derivation
  from it and redaction is not how wrong knowledge is removed.

- **retention**: Implement the whole of Section 10
  ([`fee89a8`](https://github.com/gaussia-labs/pyboltzmann/commit/fee89a8b0b87ba0595ab5e1135bfd814ae2c4839))

BrainRetention goes from 0/6 to 6/6. drop, drop_by_producer, supersede, demote, prune and redact,
  plus plan_drop, which reports a cascade before anything is written so a policy can hold a large
  one for review instead of discovering its size afterwards.

**Drop rewrites the composition and never a block.** A new Merkle DAG over the survivors, a new
  root, indices rebuilt, the removal recorded. Consumers of the new root never see the dropped block
  while older retained roots keep verifying exactly as before, which is the property that makes
  exclusion usable for wrong knowledge rather than a hole punched in history.

**The cascade walks two kinds of edge, not one.** Canonical is privileged: the paper's own example
  is a wrongly ingested PDF whose derived definitions have to go with it, so the closure over
  derived_from is always walked and every block that cited the evidence is dropped in the same
  commit. But the validation gate requires a derived block's evidence to be canonical, so no derived
  block cites another that way. What links them is structural -- a semantic block's relations and a
  procedural step's uses -- and those live on the block, which is what makes the second edge
  computable without a graph engine. It is transitive, or a drop would leave a dangling reference
  one hop further out.

A cascade cannot rewrite an append-only module through the back door: every module it reaches is
  authorized separately, so a canonical drop that would reach episodic memory fails rather than
  rewriting the record of what happened.

**Re-derivation is never the default.** Dependents are dropped even when a replacement canonical is
  given, because a citation is part of a block's identity and one citing excluded evidence cannot
  stay. The plan reports which ones could be regenerated and against what.

**Prune follows what a snapshot names, not only its block ids.** A source blob is named by a
  canonical block rather than by a composition, so reachability has to follow that hop -- otherwise
  the first prune after a drop would destroy the evidence a retained root still points at. Defaults
  to a dry run, because it cannot be undone.

**Redaction keeps the identity and destroys the bytes**, including the observed bytes a canonical
  block describes: redacting the descriptor and leaving the source readable would redact nothing.
  Membership still verifies afterwards, and resolvability reports the block as tombstoned rather
  than missing, so a lawful erasure is never mistaken for a corrupt store. The record goes in before
  the bytes go out.

- **sandbox**: Add a hybrid planner and the brain factory
  ([`3f7b4e7`](https://github.com/gaussia-labs/pyboltzmann/commit/3f7b4e7e8fa5a3381eb8af577ca3d384e2f455b8))

Fuses three rankings with Reciprocal Rank Fusion -- the scan's term coverage, the inverted index,
  the vector index -- and delegates filtering, resolution and verification to the SDK's own scan.
  That split is the paper's: candidate ranking is the planner's, verification stays with the
  protocol. A planner that reimplements verification is a planner nobody can trust.

RRF's classic offset of 60 is tuned for TREC runs of a thousand documents; against a module of a few
  dozen blocks it puts every result within two percent of every other, so the ordering is right and
  the score is useless. k=4 restores the spread at the sizes a brain produces.

This does not make retrieval faster -- the scan is still linear. It makes ranking better and runs
  the index paths. A planner built for scale would generate candidates from the index and verify
  only those.

- **sandbox**: Add a preflight check for the environment and registry
  ([`2bb9039`](https://github.com/gaussia-labs/pyboltzmann/commit/2bb9039481841c2d7ea94088c23f558c27c723ab))

The server validates its configuration in its lifespan, which is correct but unreadable: over stdio
  a startup failure reaches the client as a broken transport rather than an explanation. The same
  checks run here first, one line each, with an exit code.

It also catches the trap that `editable = false` sets. Editing ../src/boltzmann does not reach this
  environment until a reinstall, so comparing timestamps reports the confusing case: code that
  changed and behaviour that did not follow.

Resolving the remote tag is the check that cannot be faked -- the same call pull makes, against the
  same reference, with the same credentials.

- **sandbox**: Add boltzmann-inspect, since a registry UI cannot render a brain
  ([`8162288`](https://github.com/gaussia-labs/pyboltzmann/commit/81622886586f21c5f1cd97e5e57fa667cef345f7))

Docker Hub classifies the artifact correctly -- the badge says ARTIFACT -- and then reports its
  content type as Unrecognized, because a registry UI can only draw the artifact types it was built
  to know. Nothing is wrong on either side, and no registry can be expected to know this one.

So the SDK draws it: modules with their block counts and both of their identities -- the layer
  digest names the bytes you transfer, the Merkle root names the version inside them -- which
  indices travel and what built them, and what a full install would cost. Read from the same
  manifest a pull reads, and from the remote it is one manifest request with no layers, because
  inspecting what a brain contains should never mean downloading it.

--local packs and describes the layout instead, which also reports whether the travelling index is
  present to publish.

- **sandbox**: Expose the filters QueryFilters already had
  ([`1535a8b`](https://github.com/gaussia-labs/pyboltzmann/commit/1535a8b0912aa320046188c4ad93ecca5b36344a))

The search tool offered memory type, subject and superseded, and stopped there, so tags and the
  recency window were unreachable through MCP -- including the one filter episodic memory exists
  for. Found while writing the walkthrough, which described a capability the tool did not have.

A malformed timestamp comes back as a tool error naming the field. The protocol fixes the format so
  two clients agree on what "before May" means, and a filter that quietly matched nothing would be
  worse than a refusal.

- **sandbox**: Expose the protocol as MCP tools
  ([`a0a8029`](https://github.com/gaussia-labs/pyboltzmann/commit/a0a80290d3e382a760eca497aabcf6ae277e8347))

Eighteen tools, one per operation, each a thin call into the SDK. Read tools carry readOnlyHint and
  drop and prune carry destructiveHint, so a client can decide what to confirm.

Ingestion is two calls and that is the design, not a limitation. open_task returns the processing
  task together with the JSON Schema the SDK emits for its candidates; the client's model writes
  against it; submit_candidates validates and commits. The rule that the external model never writes
  to a Merkle DAG or an index becomes structural: there is no tool that would let it, and a bad
  proposal comes back rejected with its code rather than stored.

drop refuses without confirm=true, because a canonical drop always cascades to whatever cited the
  evidence and the plan is one call away.

Tools run in a thread pool, so brain access is serialized: a read overlapping a commit would observe
  a half-written version.

- **sandbox**: Implement the two index engines the SDK leaves open
  ([`c59dea3`](https://github.com/gaussia-labs/pyboltzmann/commit/c59dea3c20c72aed323ec82613f0366ac7f1dd7a))

Which engine backs an index is the implementation's choice (§6.3), so the SDK ships none -- and the
  paths that use one were exercised only by test doubles. These two run them for real.

The vector index is the one that matters: it is the only kind no client can rebuild, because
  rebuilding needs an embedding model a model-agnostic client does not carry. So it reports
  rebuildable = False, satisfies TravellingIndex, and travels inside its module's layer recording
  what produced it. Feature hashing keeps that honest without a download: the dump is byte-identical
  across platforms, so the layer digest is reproducible, and a mismatched model tag is refused
  exactly as a real embedding model's would be.

The similarity is lexical in disguise. That is the trade a sandbox should make, and swapping in real
  embeddings means changing _embed and MODEL_TAG together.

- **sandbox**: Report whether a push would carry the vector index
  ([`74a591b`](https://github.com/gaussia-labs/pyboltzmann/commit/74a591bf67c0ab62951b98a5105bf2da4a3c6ad5))

Omitting an index the brain cannot vouch for is the right behaviour and an easy one to miss: the
  push succeeds, the artifact is valid, and a consumer's semantic search is quietly worse. The
  doctor is the tool built for saying so before it happens, and it says what to do about it.

- **sandbox**: Run the whole lifecycle end to end, with assertions
  ([`50d5e2b`](https://github.com/gaussia-labs/pyboltzmann/commit/50d5e2b26ccfe1ec42666ccf853d3700ade1d635))

Register, ingest, search, prove, supersede, publish, install into a second empty brain, drop the
  evidence and watch the cascade, prune -- against whatever BOLTZMANN_REGISTRY names, so the same
  code runs against a local registry:2 and against Docker Hub.

Every step asserts. A demo that prints without checking is a screenshot: it looks like evidence and
  proves nothing. The two that matter most are that the installed version carries the digest that
  was published -- a round trip that changes the digest means the artifact is not the version -- and
  that the travelling index arrives byte for byte with its model tag.

The proposer is deterministic and carries no model, and proposes only the facts whose statement
  appears in the bytes it was handed. That keeps the run reproducible without being a fiction: hand
  it a different source and it proposes less.

Output is flushed, because piped into a log a run that hangs would otherwise show nothing rather
  than the step it hung on.

- **sandbox**: Scaffold a sandbox outside the package
  ([`f0dc456`](https://github.com/gaussia-labs/pyboltzmann/commit/f0dc4566400db0f5a066df05eddd6915e8f5867d))

The SDK's own suite runs against the source tree, so three things it cannot prove: that the built
  distribution is complete, that a real registry accepts the artifact, and that the code paths
  behind QueryPlanner and Index work when something real implements them.

This is a separate uv project for exactly that. It installs boltzmann from `..` with `editable =
  false`, so it exercises the packaged SDK -- which is how a data file that fails to ship becomes
  visible.

Configuration is validated up front and refuses to default: a brain that cannot say which OCI
  artifact it publishes to is a brain you cannot test, and finding that out after startup surfaces
  it as a failed tool call instead of a failed launch.

- **store**: Make the on-disk brain an OCI image layout
  ([`e287eb8`](https://github.com/gaussia-labs/pyboltzmann/commit/e287eb8c96c55a606eb739328e75a5eb21a6f8ba))

The paper distributes a brain as an OCI Artifact (Section 7) but says nothing about local storage.
  Making the local brain an OCI layout directly, rather than a private format converted at publish
  time, means publishing is a copy: selective installation and incremental update fall out of the
  layout instead of being re-implemented over it, and digest-based deduplication is the filesystem's
  job.

Two levels of content share one store and the distinction is kept: blobs are transportable bytes
  addressed by OciDigest, blocks are knowledge addressed by BlockId. Reading bytes is
  level-agnostic, because physical resolution does not care what a digest means. Reading a block is
  not: it decodes, checks the bytes are canonical, and hands back a typed object.

Content is immutable, so a brain still needs exactly one mutable cell for which snapshot is current.
  Keeping it outside the content-addressed space in a sidecar directory is what lets a commit be
  atomic later: blobs are written first and the pointer moves last.

Derived indices live in that same sidecar and deliberately outside blobs/, because they are views
  that can be rebuilt and no root commits to them.

A tombstoned block stays distinguishable from a missing one, which Section 10.6 requires so a lawful
  erasure is never mistaken for a corrupt store.

### Testing

- **conformance**: Add the importable suite and golden vectors
  ([`c6e2ccf`](https://github.com/gaussia-labs/pyboltzmann/commit/c6e2ccfae3ad7dd12446126379200639fcc88886))

Because the brain is portable data addressed by a protocol, the same snapshot must be readable by
  any conforming client (Section 7) -- and "conforming" only means something if it can be checked.
  The suite is importable, so a third-party store subclasses BlockStoreConformance and inherits the
  behavior the protocol requires rather than the behavior this SDK happens to have.

The golden vectors are plain JSON and ship inside the wheel, so an implementation in another
  language reads the same cases and must reach the same block_id and Merkle roots. That is the only
  practical way to establish that two clients agree on identity rather than merely claiming to. They
  cover every memory type, tree sizes around the powers of two where an off-by-one would hide, an
  inclusion proof per leaf, and the safe-integer boundary.

The suite runs here against both stores, which must be indistinguishable through the interface. A
  test also pins the vectors against the kernel, so a change that would alter an identity fails
  before the published vectors go stale -- and if such a change is deliberate it needs a new
  serialization identifier, not a regenerated file.

- **conformance**: Add the reader contract to the suite
  ([`2cbc34f`](https://github.com/gaussia-labs/pyboltzmann/commit/2cbc34fa1390578b238cd0a4b64ec9ea1affb63a))

The suite covered identity, Merkle, compositions and stores, but not the level a client actually
  implements. A third-party reader in another language had no way to check itself against Sections
  9.2 and 10.6.

Sixteen assertions on what any reader must do: report what is installed and refuse what is not,
  resolve members and refuse non-members, prove membership against the right root and fail against a
  different one, verify itself, tell tombstoned apart from missing, and return verified data with
  its provenance and never prose.

What it does not assert is a ranking order, because the protocol guarantees verifiability and not
  identical ranking -- and the no-match test uses long distinctive terms on purpose, since how a
  client treats short terms or stopwords is exactly the sort of thing the paper leaves open.

The error type is required to be a BoltzmannError. The SDK owns the exception hierarchy, so that
  much is protocol: a caller has to be able to catch a protocol failure without knowing which client
  produced it.

Run here against the SDK's own client, because a suite nobody passes is not a specification.

- **sandbox**: Cover the planner's contract and the built distribution
  ([`f169168`](https://github.com/gaussia-labs/pyboltzmann/commit/f169168ffde30568d1cf34b5736ef1d414618aa9))

The planner tests are aimed at what a planner may not decide: a match it did not verify, a block a
  filter excluded, an identity lookup reordered by approximate rankings. Ranking quality gets a few
  tests; verification gets the strict ones.

The package tests are the ones the SDK cannot run about itself. Every test in pyboltzmann/tests
  reads the source tree, so a data file that fails to ship is invisible from inside -- the import
  works and the wheel is missing it. These build a wheel and look inside, and assert the installed
  boltzmann does not resolve into ../src, without which they would prove nothing about the
  distribution.
