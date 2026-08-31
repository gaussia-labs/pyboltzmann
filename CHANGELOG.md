# CHANGELOG


## v0.9.0-b.3 (2026-08-31)

### Features

- **authenticity**: Name who a trusted key belongs to
  ([`365104a`](https://github.com/gaussia-labs/pyboltzmann/commit/365104ad93aaf6ad8222079f742cd83f158afb51))

The paper makes this connection load-bearing -- Section 5 says a signature is what turns a declared
  actor into an authenticated identity, and Section 8.3 rests the Ed25519 strictness on it -- and no
  mechanism existed. A trust-root entry carried five members and none was an identity, the SSH
  comment is deliberately stripped, and Brain.sign never reads the actor. So provenance named a
  person, the signature named a fingerprint, and nothing asserted they were the same.

TrustedKey gains an optional subject: an actor identifier, inside the signed bytes, changed only by
  a revision and therefore only by a quorum. The claim is narrow on purpose and stays narrow: it is
  what this brain's governance asserts, not a certificate, and the grounds for believing a key is
  someone's remain outside the protocol. What it adds is that once those grounds exist the
  conclusion is written where a verifier reads it.

An absent subject is omitted rather than serialized as null, so a trust root that names none keeps
  exactly the digest it had and every pin still holds.

SignatureVerdict and Authorship report it, so a quorum's holders are all readable rather than just
  the first. An attributable key reports none, which is the whole state: nobody here has said whose
  it is.

AgentSigner stops discarding the agent comment and offers it as a suggested subject when it already
  is an identifier. Offered, never adopted -- the comment is a label the key's own holder typed.


## v0.9.0-b.2 (2026-08-31)

### Features

- **provenance**: Record everyone who took part
  ([`6b74f6f`](https://github.com/gaussia-labs/pyboltzmann/commit/6b74f6f497d78da3e9784a1a9f40830c90b54e62))

Most brains are hydrated through an agent, so the record of who actually did the work was the
  missing part. A record has always named an actor; what it could not say is that a model wrote the
  interpretation, which harness it ran inside, or that a second person was in the session. producer
  answered part of that, for derivations alone, in a shape that made a version string load-bearing.

Schema version 2 adds assisted_by: people and agents in one list, each agent naming the model it
  ran, so the pair stays intact when several agents write into one snapshot. The same model under a
  different harness is a different collaborator. No version strings -- a version is the member most
  likely to be invented by whoever fills the record in, and it buys less than the identity beside
  it.

A derivation's two versions are disjoint rather than nested, since version 2 replaces producer
  instead of adding to it. DerivationRecordV2 is therefore a sibling, as SemanticBlockV3 already is,
  and requires assisted_by: version 1 obliged a writer to say what produced a derived block and
  version 2 must not relax that. A record naming nobody keeps the bytes, and the block_id, it had
  before version 2 existed.

A removal never leaves version 1. It is the one record a verifier must decode to decide a blocking
  question, and _reachable_removals skips what it cannot decode -- so an older client would read a
  valid brain, miss the record, and reject the snapshot for violating an invariant it satisfies. Not
  being able to read something must never be reported as that thing being wrong.

Ledger.made_by resolves one query across both shapes, because a brain holds records of both at once
  and a batch invalidation that read one would silently miss blocks. A person is never matched as a
  model: a human collaborator carries none.

Corpus 1.1 vendored, which is what registers provenance 2.

### Testing

- Measure the merkle scaling bound with the fastest run, not one run
  ([`5351583`](https://github.com/gaussia-labs/pyboltzmann/commit/53515835134a6b57d2f827c14065cbd296d74abe))

The assertion is narrow on purpose: doubling the leaves costs twice as much when verify is linear
  and four times when it is quadratic, so the bar sits between at three. That leaves it about 50% of
  headroom over the real ratio, and a shared CI runner spends more than that preempting the process
  -- twice on 3.13 the same code that measures 2.05 locally measured 3.5 and failed, while passing
  on 3.11 and 3.12 in the same run.

Timing noise can only make a run slower, never faster, so the minimum of several runs is the closest
  reading to the work actually performed. Five repetitions bring the measured ratio to 2.17 with a
  variance of 0.01, and cost a few milliseconds.

A fresh tree per repetition, because MerkleTree memoizes its internal nodes: verifying one instance
  twice would time a warm cache and report a speed nothing in production sees. The leaves are built
  once, outside the timer, since building them is not what is being measured.

Widening the bar was the alternative and is worse. At four it stops distinguishing linear from
  quadratic, which retires the assertion instead of stabilising it. Checked against a deliberately
  quadratic stand-in, which still measures 4.07 and still fails.


## v0.9.0-b.1 (2026-08-31)

### Features

- **identity**: Give an actor an identifier two implementations resolve
  ([`a22853f`](https://github.com/gaussia-labs/pyboltzmann/commit/a22853f655e8356d543b88723650ae26ce1e1adc))

Actor.id was an unconstrained string, and the repository spelled it five ways: role nouns, a first
  name, $USER. A provenance record is a block, so that string is hashed into block_id -- two
  spellings of one person are two names for one fact, and neither party fails. It is the divergence
  canonical serialization exists to prevent, arriving through a field nobody had canonicalized.

An identifier now takes one of two forms: an address, or a namespaced name. Lowercase ASCII, refused
  rather than normalized, because lowering one would mint a block_id the caller neither asked for
  nor can predict.

The check is deliberately asymmetric. Actor itself stays permissive, since every record ever written
  decodes through it and a validator on the type would strand every brain that predates the rule.
  Enforcement attaches where an identifier is chosen -- Brain.__init__ and the request models --
  where a caller can still be told what to choose instead.

The sandbox derived its actor from $USER, a name that resolves on one machine and nowhere else; the
  fallback is now namespaced under sandbox/ to say so.


## v0.8.0 (2026-08-31)


## v0.8.0-b.13 (2026-08-30)

### Features

- **blocks**: Warn when a schema the registry does not carry is defined
  ([`59534b2`](https://github.com/gaussia-labs/pyboltzmann/commit/59534b253fd7dfdcc9e7e0cddbb366ec318aaa5e))

Defining a block class registers its schema with this process, and nothing said whether the protocol
  had registered it. Those are different claims, and schema_version sits inside the envelope and
  therefore inside block_id -- so a schema only one deployment knows produces blocks only that
  deployment can name. Two parties holding identical knowledge compute different identifiers for it,
  which is the silent divergence canonical serialization exists to prevent, arriving through the
  version field instead.

Registration is now checked against the companion the corpus publishes, which is what the paper
  means by registered.

It warns rather than refuses, because defining a schema is how one comes to be proposed for
  registration in the first place, and an exception would make the SDK unusable for the work that
  precedes it. What must not happen is for it to pass unremarked and leave a deployment with a
  private registry it never chose. A missing or unreadable companion is ignored rather than fatal:
  this check exists to prevent a silent divergence, not to become one more way an import can fail.

### Refactoring

- **conformance**: Consume the published corpus instead of owning it
  ([`2c38bbf`](https://github.com/gaussia-labs/pyboltzmann/commit/2c38bbfe69b602fa62f7702a5acfd83dff9e1072))

The vectors lived in this package, so their location, naming and shape were governed by a Python
  layout, and "conforming" quietly degraded into "matches pyboltzmann, bugs included". The paper
  promises data readable without executing any implementation of the protocol, and a corpus an SDK
  owns cannot make that promise about itself.

The corpus now lives at gaussia-labs/boltzmann-conformance and is vendored here at CORPUS_VERSION.
  Vendored rather than fetched, so a plain pip install still carries the vectors and a reader in
  another language still needs no Python -- and a CI job diffs the copy against what is published,
  because a vendored copy nobody checks is just the old arrangement with extra steps.

Two categories the paper names had never been authored and arrive with it: schema selection, where
  oldest-that-fits is checked against the registered set rather than against whatever this package
  implements, and reconciliation, with Equation 4 alongside the two refusals. The existing
  categories gain the cases the amended paper names -- a non-BMP object key, an NFC/NFD pair,
  duplicate-key and lone-surrogate documents that MUST be rejected, duplicate-leaf collapse, and
  proofs at sizes one and a power of two.

The schema registry companion ships too. Without it, "registered" would mean "whatever this package
  implements", which is the per-deployment registry the protocol forbids -- arrived at by accident
  rather than by decision. Two tests now pin the set both ways.

The generator moves to the corpus repository, where its output belongs.


## v0.8.0-b.12 (2026-08-30)

### Bug Fixes

- **module**: Omit the tombstones member when a module has none
  ([`bdcb06f`](https://github.com/gaussia-labs/pyboltzmann/commit/bdcb06fdf074bc53314d1885274811cae5457ecb))

Every new module reference carried "tombstones":[], and the paper's snapshot carries the member only
  for a module whose composition "still names destroyed bytes". An empty list and an absent member
  are different documents with different digests, so an implementation following the paper and this
  one would have computed different snapshot digests for identical brain state -- the silent
  divergence canonical serialization exists to prevent, re-entering through a field added to prevent
  a different one.

The empty list was doing a second job, and that is the actual defect. The removal invariant used the
  member's presence to decide whether a snapshot was "modern" and had therefore opted into the
  check, which made a fact about destroyed bytes double as a protocol-version marker -- and let
  anyone turn the invariant off by omitting the field. The invariant is a statement about
  compositions and now applies unconditionally.

That leaves the case the version gate was standing in for: an unresolvable first parent, where no
  difference can be taken. It is now reported as undecidable and does not block. Refusing would
  refuse every brain that pruned its history, which the protocol permits; passing in silence would
  let a truncated history disable the check. Saying which of the two happened is the only honest
  answer.


## v0.8.0-b.11 (2026-08-30)

### Bug Fixes

- **authenticity**: Key the trust pin on the genesis digest
  ([`334a148`](https://github.com/gaussia-labs/pyboltzmann/commit/334a148179e41faf0ba86efc3e34cfeeda2d742b))

TrustPin.genesis was written at every pin and read nowhere. Its docstring promised that a brain
  which moved repositories would still be recognized as the same brain, and nothing implemented that
  -- the pin was judged entirely on the trust root, which is precisely the thing that rotates.

A brain's identity is the digest of its genesis: tags are re-assignable and the trust root changes
  by design, so two snapshots are the same brain exactly when they resolve to the same genesis. The
  verifier now checks that first. Without it an anchor taken for one brain could be evaluated
  against another's chain, and both possible answers would be about the wrong question.

The condition gets its own finding rather than reusing the trust-root mismatch, because the
  operator's remedy is different: one says authority moved in a way the pin does not approve, the
  other says this is not the brain you pinned. Pull refuses it before transferring any module layer,
  alongside the checks already there.

An unresolvable genesis is undecidable rather than negative. A legitimately pruned history cannot
  answer the question, and refusing it would punish pruning the protocol permits; the truncation is
  already reported on its own.


## v0.8.0-b.10 (2026-08-30)

### Bug Fixes

- **module**: Reject composition documents not in canonical form
  ([`5051f09`](https://github.com/gaussia-labs/pyboltzmann/commit/5051f092a25ec1b82a5d11d82ef93725760f4925))

Every other wire document this SDK decodes re-serializes itself and refuses bytes that do not match
  -- blocks, snapshots, signature records, projections. The composition document was the exception,
  and it is the one where the gap is easiest to exploit quietly: the Merkle root commits to the
  *set* of leaves, so a pretty-printed document, a differently ordered one, and one carrying an
  unknown member all produce the identical root under three different OCI digests. A snapshot names
  the digest, so one logical version could ship under several identities, each verifying perfectly.

The check goes last, after the specific ones, because it subsumes them and says nothing about which
  the caller actually tripped.

Pointer reads get the same treatment for the same reason. They were written with canonicalize and
  read with model_validate_json, so the write path refused ambiguity the read path accepted -- and
  the pin is the anchor every other authenticity judgement is measured against.

Also corrects the golden-vector docstring, which named a regenerate() function that has never
  existed.


## v0.8.0-b.9 (2026-08-30)

### Features

- **authenticity**: Warn when the govern quorum leaves no margin
  ([`d421559`](https://github.com/gaussia-labs/pyboltzmann/commit/d4215599ec824238aa52378de523e63795951d76))

A trust root whose quorum equals its number of govern holders is legal, and it is also a one-key
  fuse. Lose that key -- stolen, or simply lost -- and governance is over: neither the remaining
  holders nor an attacker can assemble the signatures to record a compromise or admit a replacement,
  while a stolen key keeps signing within its scopes. The protocol has no recovery path, because
  re-anchoring would be exactly the self-assertion the quorum rule exists to forbid.

So it is said out loud, twice and for different readers. init and rotate warn at the moment the
  margin is chosen, which is the only moment anything can still be done about it, and the report
  carries a non-blocking QUORUM_MARGIN finding so a consumer meeting the brain later sees the
  condition too.

A warning rather than a refusal: no rule forbids the configuration, and a deployment with exactly
  one owner has no other option available to it.


## v0.8.0-b.8 (2026-08-30)

### Features

- **authenticity**: Distinguish an attributable proposal from an unauthorized head
  ([`1c52fae`](https://github.com/gaussia-labs/pyboltzmann/commit/1c52fae39528ddcacee918b4ffd58d041b010dd0))

A signature by a key the trust root does not list had exactly one reading here, and the protocol
  requires two. Offered for review, such a snapshot is how an open project hears from someone it has
  never admitted: the author is identified and no authority attaches. Served as the brain's current
  state, the identical bytes are an impersonation attempt. Collapsing them meant either refusing
  every stranger's contribution or reporting an imposture as an ordinary proposal.

The distinction is positional, so the position is now an input. authenticate() takes a stance,
  defaulting to HEAD because a caller who does not say is asking about a brain's current state and
  must get the safe answer. Under OFFERED an unlisted key yields ATTRIBUTABLE_KEY and the report
  resolves to the new ATTRIBUTABLE state; the policy bar for a published head is not applied, since
  judging a proposal against it would refuse every contribution ever made.

Attributable is not a weaker authorized: require_authorized still raises. What it adds is the
  author's fingerprint, on the report and in the Authorship an evidence bundle carries, because a
  state whose whole content is "who wrote this" that did not say who would have gained nothing over
  an anonymous one.

plan_reconcile sets the stance for the contribution path and reports the result, which is where a
  maintainer reads it.


## v0.8.0-b.7 (2026-08-30)

### Features

- **ingest**: Record the verdict that admitted each block
  ([`aaaede6`](https://github.com/gaussia-labs/pyboltzmann/commit/aaaede664240ce91289f1e589152afdf88134219))

The provenance ledger carried six record types where the protocol names seven. The missing one is
  validation, and its absence made "it was validated" a claim a consumer had to take from whoever
  committed: the verdict lived only on the write path, and nothing in the signed composition could
  confirm a gate had run at all.

Every committed block now gets a validation record beside its derivation edge, naming the verdict,
  the checks that produced it, and the task. The check set is part of the claim rather than
  decoration -- the same VALIDATED under two different check sets says two different things -- so
  the gate now carries the codes that ran on its report, and the record refuses to be written
  without them.

ValidationStatus moves to the provenance module, because a verdict that travels in a record is wire
  schema rather than write-path bookkeeping. It is re-exported from the gate, so every existing
  import keeps working.

Brain.audit_validation reads the ledger back and reports what cannot show its verdict. It reports
  rather than refuses: a brain written before the record existed did nothing wrong, and refusing it
  would trade availability for an auditability that snapshot cannot retroactively supply. The
  removal invariant is the one that rejects, because there a missing record is the attack itself.


## v0.8.0-b.6 (2026-08-30)

### Bug Fixes

- **distribution**: Serialize a projection's references as its source does
  ([`088cffd`](https://github.com/gaussia-labs/pyboltzmann/commit/088cffdf1c6f7f1fdd44cacb89ef694b2de1e6e2))

A projection's canonical bytes came from a plain model dump while a snapshot's exclude None, so a
  retained reference was spelled with explicit nulls where the source snapshot omitted the keys
  entirely. The two documents then disagreed about a reference both claim is the same one, and a
  consumer comparing the retained entry against the resolved source byte for byte would have been
  right to refuse.

Adding tombstones to ModuleRef widens that gap by one more optional field, so it is fixed here
  rather than left to grow.

### Features

- **retention**: Make removals verifier-checkable
  ([`67a67c9`](https://github.com/gaussia-labs/pyboltzmann/commit/67a67c9533b3042432a588daeaccf2d7e7337a0a))


## v0.8.0-b.5 (2026-08-28)

### Features

- **distribution**: Add typed projection configs
  ([`c0f279b`](https://github.com/gaussia-labs/pyboltzmann/commit/c0f279bbb2c7ddd68486682fd3bf6bea68ff81cc))


## v0.8.0-b.4 (2026-08-28)

### Bug Fixes

- **distribution**: Refuse pull rollbacks
  ([`4090776`](https://github.com/gaussia-labs/pyboltzmann/commit/4090776d8478b42bb3f17d4d6d96a5867a83ccc7))


## v0.8.0-b.3 (2026-08-28)

### Bug Fixes

- **authenticity**: Enforce SSHSIG and key security floors
  ([`24b3dfe`](https://github.com/gaussia-labs/pyboltzmann/commit/24b3dfe7c9a8673cecf6b40074275e4d8de09c68))


## v0.8.0-b.2 (2026-08-28)

### Bug Fixes

- **identity**: Reject ambiguous wire documents
  ([`a2aebec`](https://github.com/gaussia-labs/pyboltzmann/commit/a2aebecb0d035f6c95130c42b813d694c2c124cd))


## v0.8.0-b.1 (2026-08-28)

### Features

- **distribution**: Bind travelling indexes to snapshots
  ([`f0646a2`](https://github.com/gaussia-labs/pyboltzmann/commit/f0646a274fe16f1ea1a41bc30d524b8896ee9cb4))


## v0.7.1-b.1 (2026-08-28)

### Bug Fixes

- **reconcile**: Refuse multiple best common ancestors
  ([`29a58dd`](https://github.com/gaussia-labs/pyboltzmann/commit/29a58dd19af7e2c12c5007deb4c6171bdb17be72))


## v0.7.0 (2026-08-28)

### Continuous Integration

- Use the GitHub Actions bot for release commits
  ([`1559450`](https://github.com/gaussia-labs/pyboltzmann/commit/15594503ed4d7c3a249d2aaa601b1d11483881e1))


## v0.7.0-b.2 (2026-08-28)

### Bug Fixes

- Harden catalog integration
  ([`a8a721a`](https://github.com/gaussia-labs/pyboltzmann/commit/a8a721a975b158f48067468691470c83301bd4f5))

### Features

- **catalog**: Add hierarchical catalog navigation
  ([`c01a306`](https://github.com/gaussia-labs/pyboltzmann/commit/c01a30623347098255ac525c396e12803b5ff759))


## v0.7.0-b.1 (2026-08-25)

### Bug Fixes

- **authenticity**: Judge revocation and truncation fail-closed across the chain
  ([`35037f1`](https://github.com/gaussia-labs/pyboltzmann/commit/35037f107824d7c8448d41f4e62b96931ffd03df))

Two ways an attacker could hide history from the verifier, closed with one doctrine: authorization
  stays derived first-parent-only, but revocation and truncation are judged over everything
  reachable.

descends_from walked first parents only, so a compromise position reachable only through a merge's
  second parent answered False -- "cleared" -- and the stolen key was re-admitted to quorums and its
  signatures kept. It now walks every parent with a three-way result: reachable withdraws,
  undecidable stays undecidable, and only a DAG whose every path closed at a genesis clears.

The ancestry walk emitted CHAIN_TRUNCATED only for the head's own missing parent. A fabricated
  revision whose first parent was simply withheld sat deeper, was labelled ordinary, and the
  self-admission attack authenticated as AUTHORIZED. A walk that ends at an unresolvable non-genesis
  position is now a blocking finding -- unless a pin matched, because a pin can only match within
  the resolvable walk, so a match is an explicit anchor at or above the gap.

AuthenticationReport.detail(kind) is now public; refusals quote it.

- **brain**: Close the pin bypass and make signature evidence travel
  ([`9f0a653`](https://github.com/gaussia-labs/pyboltzmann/commit/9f0a6534a3d78e14c298621f20b35dc8dad61ecb))

Six review findings against the facade's distribution trust path, fixed together because they share
  one surface:

The pin gate accepted on the manifest's trust-root annotation -- registry-controlled input --
  without ever authenticating the config, and its fallback consulted only TRUST_ROOT_MISMATCH, so an
  unapproved rotation descending from the pin installed anyway. The annotation is now diagnostic
  only: the config, history, and signatures are always fetched (all small) and the gate refuses on
  quorum failures, revision violations, and blocking truncations before any module layer moves.

countersign() guarded all five refusals behind "has a parent", signing any fabricated genesis
  outright. A parentless document is now refused unless this brain already holds it, keeping the
  legitimate pull-then-endorse bootstrap, and since-claims are checked on both branches.

Only head-keyed records travelled, but the verifier demands quorum records over every revision
  ancestor -- so every legitimately rotated brain arrived unauthorized. Publish now walks the
  custody set (head, revision ancestors, governed genesis) and merge accepts exactly the artifact's
  own ancestry, never whatever the store happens to hold.

A subset publish minted a projection nobody signed and nobody could sign, so it always exported
  unsigned. The projection is now anchored to the source head the existing annotation names:
  validated as a byte-exact subset (fail-closed), then judged through the source's own records,
  which travel with the subset artifact.

Merging referrers could be aborted by the 513th record (the cap raises an AuthenticityError no
  except clause covered) or an unlistable referrers endpoint; both now skip with a warning, bounded
  by a per-call merge ceiling, honoring the method's own contract.

"Previously seen signed" was read off the current local head, so one local commit disarmed the
  stripping guard and locally-signed stores false-positived on honestly-unsigned upstreams. It is
  now a persistent per-repository pointer, written only when a pull verified AUTHORIZED, first
  sighting kept forever.

Registry-supplied config bytes are parsed through a wrapper that turns a model mismatch into a
  DistributionError naming the upgrade path.

- **distribution**: Gate governed artifacts behind a declared wire version
  ([`d15bb45`](https://github.com/gaussia-labs/pyboltzmann/commit/d15bb4550cc783b609e993287f3fd9235544f05d))

trust_root rides in the config blob, and Snapshot is extra="forbid" on every already-shipped client
  -- so an old client passed the exact-match version gate ("all good, version 1"), downloaded blobs,
  and died mid-transfer on a pydantic traceback.

A global PROTOCOL_VERSION bump is off the table: it is embedded in every block envelope, so it would
  change every block_id and invalidate the published golden vectors. Instead a governed artifact
  declares WIRE_VERSION (2) in the manifest annotation old clients already refuse on, so they fail
  fast at resolve() with their existing clear message, before any blob moves. Ungoverned artifacts
  keep declaring 1, so their interoperability is untouched. This client's gate relaxes from
  exact-match to declared-above-supported, and a manifest that passes the gate but does not fit the
  model is refused as a DistributionError naming the upgrade path instead of leaking a validation
  traceback.

- **distribution**: Read referrers listings tolerantly, keep fallback entries intact
  ([`9318da4`](https://github.com/gaussia-labs/pyboltzmann/commit/9318da4f963780b5be6bf1d21199e0d86ddee96f))

A referrers listing is shared, unauthenticated space: other tools attach SBOMs and attestations
  whose descriptors legally carry fields this SDK does not model (platform, urls, data). Validating
  every entry under extra="forbid" before the artifactType filter let one foreign sibling crash
  pull() with a raw ValidationError, and a non-JSON body escaped as a JSONDecodeError. Entries are
  now filtered as raw dicts first, parsed second, and skipped with a warning when unreadable --
  matching what LocalLayoutRegistry already did.

The fallback-tag append also round-tripped foreign entries through this client's model, silently
  dropping the fields it did not know; the index is now carried as raw documents, so a rewrite drops
  nothing.

### Continuous Integration

- Pin the release tooling instead of rebuilding it unpinned every run
  ([`d7b1213`](https://github.com/gaussia-labs/pyboltzmann/commit/d7b12132180b75d5d2bc83eabb0dfee1f8048190))

The python-semantic-release and publish-action Docker actions pip-install their dependencies at
  image build time, on every run, with no lockfile. GitPython 3.1.60 (published 2026-08-25 18:33
  UTC) removed the Actor.name_email_regex attribute that every python-semantic-release version reads
  while parsing its configuration, and the develop release job died 13 minutes later with no change
  on our side.

Both steps now run semantic-release from a venv this workflow installs itself, with
  python-semantic-release and gitpython pinned. The venv is seeded with pip because build_command
  expects one, the way the action's container provided it. Outputs are unchanged: semantic-release
  writes released/version/tag to GITHUB_OUTPUT itself whenever the variable is set, action or no
  action.

The gitpython pin can be dropped once python-semantic-release stops using the removed attribute.

- **docs**: Apply the navigation the sync manifest already declares
  ([`5f56dc9`](https://github.com/gaussia-labs/pyboltzmann/commit/5f56dc9b31a062a593d30002e412cca0381781a3))

The reconciliation guide synced to the central docs and shipped unreachable: the file landed,
  nothing in docs.json pointed at it, so the page existed and no reader could get to it.

docs-sync.json already described the whole tab -- every group, every page -- and the workflow read
  only its target_dir. The navigation was therefore maintained twice, once as a declaration nobody
  applied and once by hand in another repository, with the workflow's own pull request body asking
  whoever merged it to remember. That is the kind of step that works until the day it does not.

The tab is now rewritten from the manifest. Only the matching tab is touched: docs.json is stored as
  json.dumps(indent=2), so writing it back that way leaves every other SDK's navigation
  byte-identical, which keeps the diff reviewable and keeps this from becoming a reason to distrust
  the sync.

It refuses rather than syncing when the manifest and the synced files disagree, in either direction.
  A page with no entry is unreachable and an entry with no page fails the docs build, and neither
  announces itself -- the first is invisible until someone looks for a guide that should be there.
  Verified against the live docs repository: on its current state the script produces exactly the
  one line that was missing and nothing else, and each refusal leaves docs.json untouched.

### Documentation

- **guides**: Document authenticity and retire the not-implemented notes
  ([`2208e1c`](https://github.com/gaussia-labs/pyboltzmann/commit/2208e1ca84a3afd87d0f531abd01a3fdbaaeb788))

A new guide covers the whole role: creating a governed brain, the quorum rule and the multi-party
  rotation flow, retirement versus compromise without clocks, the pin, publication as referrers, the
  verification policy, and what a plain install can and cannot decide. The navigation gains the page
  in both docs.json and the sync manifest, which must move together.

The reconciliation guide's "not implemented -- needs authenticity" row becomes the
  GovernanceConflictError it now is, and the notes that said no signing exists say what signing
  mechanically guarantees instead. The conformance guide lists the two new vector files and the new
  suite; the interfaces page gains the sixth role and the sixth exception family.

### Features

- **authenticity**: Add SSHSIG signing, trust roots, and the positional verifier
  ([`5f0c1ba`](https://github.com/gaussia-labs/pyboltzmann/commit/5f0c1ba6939f1d42e768731c2f7ba03ca5f6b921))

The paper's Section 8 as a package: detached SSHSIG signatures over canonical snapshot bytes --
  byte-identical to ssh-keygen's output and verified against OpenSSH in both directions -- a trust
  root whose validity is positional rather than temporal, required scopes computed from the
  difference a snapshot made rather than from what a signature claims, and an authenticator whose
  report keeps integrity and authenticity separate: state is a derived property, so no construction
  can claim authorized while carrying a blocking finding.

Signing goes through ssh-agent only. The private key never enters the process, hardware tokens
  included, and the agent returns exactly the RFC 4253 blob SSHSIG's signature field wants, so
  nothing is reframed.

The Ed25519 mathematics is the one thing the optional [authenticity] extra buys. Every structural
  check -- wire framing, fingerprints, the fingerprint-versus-embedded-key rejection the paper
  requires of every reader, quorum arithmetic -- is standard library and works on a plain install,
  and an unchecked signature reports unverifiable: "could not check" must never read as either
  verdict, or an attacker's uninstall becomes a forgery.

The snapshot-facing halves (scope diffing, the chain walker, the authenticator) land here and
  activate when the snapshot learns to carry a trust root in the next commit.

- **brain**: Authenticate, sign, pin, and govern through the facade
  ([`4738808`](https://github.com/gaussia-labs/pyboltzmann/commit/47388080caa6ab33fab63bf5d4b851dd07a704be))

Brain.init creates the genesis -- the single point where authority is asserted rather than derived,
  anchored by pinning rather than validated. sign produces a detached record whose claimed scopes
  default to what the snapshot actually required; authenticate runs the top-down check and returns
  the report, with compromise markers read from the head's trust root because a compromise is
  recorded later than the positions it withdraws; pin records the one thing that comes from outside.

Governance moves only by quorum, evaluated against the key list being replaced. A single owner
  rotates in one call. A quorum spanning machines uses plan_rotate -- the revision document is built
  once, because created_at makes two constructions sign different bytes -- then countersign on the
  exact bytes received, which refuses mechanically what a reviewer would refuse by reading: an
  unseen parent, content smuggled into a governance act, a non-advancing trust root, an admission
  claim the observable chain refutes. rotate verifies the quorum before the head moves; a failed
  quorum advances nothing. revoke builds the revision that retires a key (its history stands) or
  records a compromise (its signatures from that position are withdrawn).

pull gates on the pinned trust root before transferring any module layer, collects referrer
  signatures, refuses a stripped brain once seen signed and a propose-scoped head unless policy
  permits it. Reconciling histories that carry different trust roots is refused outright as a
  governance conflict: unioning two key lists grants the union of both sides' permissions. Every
  query result carries authorship beside verified, never folded into it. Pruning keeps signature
  records of retained snapshots -- a signature a garbage collection can remove is not a signature.

- **conformance**: Publish golden vectors for the authenticity role
  ([`f45b90a`](https://github.com/gaussia-labs/pyboltzmann/commit/f45b90a98a88df44730d0e2a14e0f56b943c615b))

Two files, two layers, both plain data readable without executing this SDK. sshsig.json pins the
  wire format byte for byte -- including the signed-data blob, which is what tells a framing bug
  from a signing bug, since without it both fail at the same place with the same message. It also
  pins the traps verified against OpenSSH 10.2: no version field in the signed data, 70-column
  armor, and the reserved-field asymmetry. signatures.json pins the judgement: whole chains,
  published test key pairs (the seeds are deliberately public), and the verdict a verifier MUST
  reach for each of the paper's worked cases -- admission by quorum, a self-admitted key failing
  with no pin at all, retirement standing where compromise withdraws.

AuthenticityConformance replays every published case against any store, so a third-party
  implementation inherits the whole judgement layer the way it already inherits the identity one.
  The generator lives in tests/ and is deterministic by construction: a regeneration that changes a
  published vector means a bug or a new format version, never noise.

- **distribution**: Publish signatures as referrers, and compositions with history
  ([`2c84cc7`](https://github.com/gaussia-labs/pyboltzmann/commit/2c84cc7a112ccc6e0d6df695d201c550a70aecdb))

A signature is never a layer of the brain manifest: adding a countersignature would change the
  manifest, and therefore the brain's digest, so a brain would change identity because someone
  agreed with it. Each record is instead the single layer of its own manifest, whose subject names
  the brain -- an OCI referrer in a registry, one more index.json entry in a local layout, so an
  export and the wire carry one format. Registries that predate the Referrers API get the
  sha256-<hex> fallback tag, read-modify-written on push. The trust root's digest is annotated on
  the brain manifest so a consumer can compare a pin before transferring anything.

Referrers are a separate RegistryReferrers protocol rather than three more methods on
  RegistryClient, because adding methods to a protocol breaks every third-party transport that
  already satisfies it; callers feature-detect, and a transport that never learned about referrers
  still moves the brain.

The history layer now also carries the composition documents its snapshots reference. Required
  scopes are computed from the difference against the first parent, and without the parent's
  compositions every pulled commit is undecidable between an ingest and a canonical drop -- a
  verifier that guessed smaller would be exploitable by shipping a truncated history. With
  compositions travelling, "never transferred" is now detected at the block level, which is what the
  diagnosis meant.

- **module**: Carry the trust root inside the snapshot document
  ([`dffdb21`](https://github.com/gaussia-labs/pyboltzmann/commit/dffdb21777ed315dcec4d96304bc4bed91b75009))

It lives in the snapshot rather than in a module or a layer because it must reach every install,
  complete or partial, and because inside the signed bytes a signature can never be evaluated
  against a key list the signer did not commit to. A None never enters the canonical bytes, so every
  existing snapshot keeps the exact digest it had before the field existed -- pinned by test against
  the pre-field serialization.

Every derivation carries the trust root forward. A commit is not a governance act: a derivation that
  dropped it would present a changed digest (present to absent) to the verifier, be classified as a
  revision, and demand a govern quorum it has no reason to carry. A reconciliation keeps the first
  parent's -- a merge does not adopt a key list. The one constructor that changes it,
  with_trust_root, copies the modules verbatim and is structurally unable to fold content into
  governance.

With the field in place, the scope computation of the previous commit becomes decidable and is
  covered here: the paper's scope table as a parametrized matrix, an executable oracle over
  arbitrary compositions, and fail-closed questions wherever evidence is missing.

- **protocol**: Declare the authenticity role and export its surface
  ([`67d46b7`](https://github.com/gaussia-labs/pyboltzmann/commit/67d46b76a2f204c09ceaafe69ceaf4273ed6af18))

BrainAuthenticity is the sixth protocol role, and it is claimable separately on purpose: a consumer
  that recomputes integrity while holding no trust anchor is not a degraded client but the
  zero-configuration case the protocol guarantees, and requiring signature verification for a reader
  to conform would make offline integrity conditional on configuration the protocol promises it does
  not need. What no implementation may do is claim an authenticity it did not check.

The package root exports the working surface -- Scope, TrustRoot, SignatureRecord,
  AuthenticationReport, VerificationPolicy, AgentSigner and the rest -- so a caller reaches it the
  way they reach every other role.


## v0.6.0 (2026-08-20)


## v0.6.0-b.1 (2026-08-20)

### Bug Fixes

- **distribution**: Refuse a history layer that misnames its entries
  ([`6752b39`](https://github.com/gaussia-labs/pyboltzmann/commit/6752b39b3590f0033a7d983f57ed2e6d49f8c266))

The entry name is redundant under content addressing -- a substituted document lands under its own
  digest, not the one the name claims, so it cannot impersonate a snapshot a lineage asks for. What
  it can do is disagree with its payload, and that surfaces much later as an unexplained "no common
  ancestor" when the parent fails to resolve.

Checked at the boundary for the same reason a stored composition is checked against the root it is
  filed under: the invariant is redundant, and one clear refusal beats a confusing failure three
  operations away.

- **reconcile**: Cascade at the step that withdrew the evidence
  ([`8072ef2`](https://github.com/gaussia-labs/pyboltzmann/commit/8072ef291304240b11021029741ea62a15003f31))

A rebase replays the other history one version at a time, but the cascade was computed once against
  their head and then applied as an exclusion at every step, while the removal records explaining it
  were written only at the last one. A contribution whose third version withdrew a source published
  two earlier versions with the dependent already gone, its evidence still present, and nothing on
  record saying why -- an unexplained removal, which is the one thing an auditable history cannot
  contain.

The cascade now follows each step's own exclusions and its record lands in the same version. Records
  this reconciliation authors are carried forward explicitly, because Equation 1 is stated against
  the version this brain was at: that side is fixed for the whole replay, so a record written at one
  step is in no later step's arithmetic and would drop straight back out. Fixing it by advancing
  that side instead would undo their removals -- a block they added and then dropped would be in the
  previous result, absent from the ancestor, and so survive.

A step's cascade is narrowed to the one the plan reported. Their removals accumulate along their
  chain so this is a no-op, but the direction it fails in matters: nothing leaves on a step nobody
  reviewed.

Precedence edges now land at the first step holding both contenders rather than at the end, for the
  same reason, and still fall through to the last step when no earlier one supports them.

- **reconcile**: Cascade through provenance when evidence is withdrawn
  ([`33e0aa8`](https://github.com/gaussia-labs/pyboltzmann/commit/33e0aa8d90f69beac8b22be0ed7dfec8da2a6b4f))

Equation 1 is applied one module at a time and is individually correct in each, which is exactly why
  it was not enough. If the other history withdrew a canonical source and this brain held a block
  derived from it, the source left and the dependent stayed behind, citing evidence the composition
  no longer holds. R1 was violated and `verify` did not catch it, because verifying recomputes
  hashes and compositions rather than citations across modules -- the same reason admitting a
  rejected block is refused.

The validation gate caught the mirror case, where the derived block is the incoming one. It could
  not catch this one: nobody proposed the block, it was already here. So the reconciliation now runs
  the cascade a drop runs, over the compositions Equation 1 produced, and the dependent follows its
  evidence out. Reusing that cascade rather than writing an evidence check for this case is the
  point -- one consequence implemented twice would eventually disagree with itself. It is not
  policy-gated, because the removal already happened in the other history and the equation is a
  statement about sets rather than a request to remove anything.

The halt covered incoming blocks only, so a reconciliation that merely removed work committed
  without asking -- a decision taken on the operator's behalf, which is what the halt exists to
  prevent. It now stops when anything this brain holds would leave, and continuing needs that
  stated. One statement rather than one per block: exclusion wins by construction, so a per-block
  choice would be false, and re-admitting a removed block stays an ordinary commit outside the
  reconciliation. The acceptance records what it covered, so it cannot answer a question that has
  since changed.

The blocks the cascade removes get a removal record of their own. The other history recorded
  withdrawing the evidence; nothing yet recorded what that cost here, and an unexplained removal is
  not auditable.

### Documentation

- Establish immutability and enumerate every reconciliation case
  ([`05574f8`](https://github.com/gaussia-labs/pyboltzmann/commit/05574f8f0e7c9747b85518e5488834a2ca312dff))

The reconciliation guide asserted that a block is never modified and then built everything on it
  without establishing it, which leaves the reader to ask the first question the design answers:
  what happens when two people edit the same block. Nothing happens, because it cannot arise, and
  the four things that prevent it are now spelled out along with what people do instead --
  supersede, contradict, drop -- each of which is a case the reconciliation handles.

The cases themselves were scattered across the page and incomplete. They are now one table,
  including the ones that were missing: identical corrections converging for free, a module absent
  on one side not being a removal, an append-only module that cannot be narrowed, and no common
  ancestor. Each row says whether the protocol settles it or a person does.

`redact` is called out where it belongs. It is the one operation that touches bytes already written,
  it destroys rather than modifies, and it is refused by the default policy and by PERMISSIVE_POLICY
  alike.

The architecture page still documented `snapshot.parent`, which no longer exists -- reader code
  copied from it would have raised. It now documents `parents`, `first_parent`, and why `ancestry`
  and `reachable_history` answer different questions. Retention gained the pointer it was missing: a
  drop travels, and reconciling with a history that dropped your evidence takes your dependents with
  it.

Replaced the two MDX components this page had introduced on its own with the ones the rest of the
  docs use.

### Features

- **distribution**: Publish the snapshot history and add fetch
  ([`cd12ed4`](https://github.com/gaussia-labs/pyboltzmann/commit/cd12ed498c00b5c6d5e6d1dbbc32d3c47a69c400))

An artifact published its head and one layer per module, so the parents a snapshot names resolved to
  nothing on the receiving side: the chain an audit walks stopped at one link, and nobody but the
  publisher could find the ancestor two histories share. The documents now travel as their own layer
  -- their own rather than loose blobs, because a blob no manifest references is unreferenced, and a
  registry is entitled to reclaim it. A snapshot document is a few hundred bytes and compresses well
  against its near-identical siblings, so the whole reachable history goes rather than a recent
  window; a cutoff would decide on the publisher's behalf how far back a consumer may reconcile
  from.

`fetch` retrieves a remote history without moving the local pointer, which is the step at which
  nothing has changed yet: two histories are held locally while the published brain is untouched.
  Separate from `pull` because judging an incoming history should not require adopting it first. It
  loads no index -- a travelling one is bound to the root it was built over, and loading it for a
  history that is not installed would leave the brain holding a stale index.

A partial install now chains to the version it was taken from instead of starting a fresh history,
  and the narrowing guard asks whether the snapshot omits a module the remote tag names rather than
  whether the install happened to be partial. That is the condition that makes a publish dangerous,
  and stating it that way is what lets a partial install be published back at all.

- **module**: Name a snapshot's predecessors in a parents list
  ([`bba620e`](https://github.com/gaussia-labs/pyboltzmann/commit/bba620e4dad17f2c7ea0f58b7b5cce6949f47a10))

A snapshot named one parent, so a history that joined two others could not be represented and
  divergence could only ever be refused. The field becomes a list: a linear history carries one
  entry, a root snapshot none, a reconciliation two or more.

Order is significant in exactly one way. The first parent is the history a reconciliation was
  performed onto, and it is the line an audit follows. Containment is a different question, so it
  gets a different method that follows every parent, and the fast-forward check asks that one --
  after merging a contribution the contributor's head is a parent of the local snapshot, and walking
  only the first-parent chain would report that as divergence.

On the wire one parent is written as the scalar `parent` and two or more as the list `parents`. A
  linear history therefore keeps the bytes and the digest it had before, and an older client stops
  being able to read a brain only at the point where that brain genuinely reconciled something,
  which is the one document it could not have interpreted anyway.

- **reconcile**: Reconcile diverged histories, resolvable by hand
  ([`9d33cc8`](https://github.com/gaussia-labs/pyboltzmann/commit/9d33cc82044d0410d356046c91cd6eeca56caa57))

Detecting divergence and refusing to overwrite was half the obligation. Refusing is safe and
  incomplete: it left the resolution to hand-editing, and it meant a partial install could never be
  published back over the tag it came from.

The structural half is set arithmetic over immutable, content-addressed blocks, so a textual
  conflict is not representable and the result converges whichever side is called ours. That is not
  enough on its own: the arithmetic is correct per module and the invariants run between them, so
  one side dropping evidence the other derived from leaves a composition that is individually right
  everywhere and violates R1 overall. The result is therefore put through the ingestion gate
  unchanged -- a conflict here is a validation failure, not a differencing failure -- and only what
  emerges validated enters.

Merge, rebase and squash compute the same blocks and differ only in the lineage recorded, which once
  snapshots are signed is a question of attribution rather than tidiness. So the strategy is
  required, has no default and no policy default, and the plan prices all three before the choice is
  made.

If anything did not apply cleanly, nothing is written. Committing the part that fits would be a
  decision about the rest taken without asking, so the reconciliation stops and records what is open
  in a pointer beside the head, and a person answers each question and continues or abandons it.
  Admitting a contradiction is available, because a contradiction is information rather than a
  defect. Admitting a rejection is not: a derived block whose evidence the composition does not hold
  cannot be audited against its source and no later check would notice, so the refusal names the
  operation that fixes the cause instead. This is where the model departs from version control on
  purpose.

Two histories that superseded the same block with different successors no longer let the ledger's
  last reader pick a winner. Both edges stay recorded, the precedence surfaces as a candidate the
  protocol will not decide, and settling it writes one more supersession edge from the winner over
  the loser -- the only way this architecture states precedence, and so no new record type.

Trust roots and the propose scope are out of reach until authenticity lands, so the two conflict
  classes that need them are documented as such rather than approximated.

### Refactoring

- Inline every TYPE_CHECKING import
  ([`1f7f8b9`](https://github.com/gaussia-labs/pyboltzmann/commit/1f7f8b98e7a2f65495a45b3e1f3eae16b7917fa7))

The blocks are gone from all 47 files that had them, along with the two pieces of configuration that
  produced them: ruff's TCH rule family in both projects, which flags a type-only import left at
  module level, and the coverage exclusions that existed only to skip the blocks.

Nothing needed to stay deferred. Every module already carries `from __future__ import annotations`,
  so annotations were never evaluated at runtime anyway, and importing each module standalone
  confirms the dependency graph has no cycle for an eager import to expose.

One block was load-bearing rather than cosmetic. `conformance/__init__` re-exports the pytest-backed
  suites through a lazy `__getattr__`, because pytest is an optional extra and a plain install must
  still be able to read the golden vectors; the block gave those names static visibility without
  importing them. Hoisting it pulled pytest into the package import and broke three tests that pin
  exactly that. The import is now dropped rather than hoisted, so the names resolve only through
  `__getattr__` -- the cost is that a type checker sees them as Any, which is what the block was
  buying.


## v0.5.0 (2026-08-18)

### Features

- **distribution**: Allow ignoring vector indices on pull
  ([`5151b27`](https://github.com/gaussia-labs/pyboltzmann/commit/5151b27421e9b0224f1cfd99ed4b1f2cd04b73bc))


## v0.4.1 (2026-08-12)

### Bug Fixes

- **blocks**: Re-export the v2 schemas from the package
  ([`322a2a0`](https://github.com/gaussia-labs/pyboltzmann/commit/322a2a0b1ee7bb87298f5aa3b487b051cf490986))

0.4.0 shipped SemanticBlockV2, EpisodicBlockV2 and ProceduralBlockV2 working -- registered, selected
  by payload, round-tripping -- and `from boltzmann import SemanticBlockV2` raising ImportError.
  Only the long path worked.

They were lost in 77e3b11. The merge conflicted on __version__ and the resolution took the whole
  file from the other side, which was master at 0.3.0 and predated the exports. Ten lines went with
  it: five imports and five __all__ entries, including NamesContent and require_media_type.

Nothing failed. The suite imports from boltzmann.blocks.* throughout, and the one test that reads
  the package surface walks __all__ -- so a name deleted from __all__ takes its own check with it.

The guard asks the question that has an answer independent of the file: every schema in
  Block.registry() must be reachable from the package. A registry entry is produced by declaring a
  class, so the two cannot drift without the test noticing. The weaker half -- that a name imported
  into the package namespace is also declared -- covers the same merge undoing half an export rather
  than all of it.

Deliberately not asserting that boltzmann re-exports all of boltzmann.blocks.__all__: nine names
  have always been module-only, the provenance record types among them, and that is curation rather
  than oversight.


## v0.4.0 (2026-08-12)

### Build System

- Keep uv.lock current through the release
  ([`6a5c08e`](https://github.com/gaussia-labs/pyboltzmann/commit/6a5c08e50234c96103f0c8720990cc85114f6497))

semantic-release rewrites pyproject.toml and src/boltzmann/__init__.py and knows nothing about
  uv.lock, so the lock recorded a version the project had already left at every release. It has been
  corrected by hand twice, and both times only because someone happened to run uv and find a dirty
  tree.

`uv lock` re-stamps only the project's own entry -- verified against a bump, where the diff is one
  line and no dependency is re-resolved -- and `assets` puts the result in the release commit. The
  order works out: semantic-release builds the path list, runs build_command, and stages afterwards,
  so what the lock is rewritten to is what gets committed.

uv is installed by the build command rather than assumed, because the release action is a Docker
  action: the uv that setup-uv puts on the runner does not exist inside it, and only python, pip and
  git do. The whole command was run in a clean python:3.11-slim container to confirm it, since
  finding out otherwise would mean a failed release rather than a failed test.

CI also asserts the lock is current. If this ever stops working the symptom is silence, which is how
  it went unnoticed twice.

- Sync uv.lock with the version in pyproject.toml
  ([`153f816`](https://github.com/gaussia-labs/pyboltzmann/commit/153f816af349c3fea7cd29049c88949ce6ab04ec))

semantic-release bumps pyproject.toml and src/boltzmann/__init__.py, and nothing updates the lock,
  so it goes stale on every release. Nothing consumes the number -- the project is an editable
  install and the entry carries no hashes -- but a lockfile that disagrees with the manifest it
  locks is a question every reader has to answer first.

### Continuous Integration

- Run the suite on pull requests and back-merge master automatically
  ([`1ed30b4`](https://github.com/gaussia-labs/pyboltzmann/commit/1ed30b4e5e66144b13392b5520c9de65cec413f9))

Two gaps that produced the same bug from opposite ends.

The workflow only ran on push to master and develop, so no pull request ever reported a check. #4
  and #5 were both merged on the strength of a local run, and a contributor without the repo checked
  out had nothing at all. The pull_request trigger carries no path filter, deliberately: a push is
  filtered because it may cut a release and a docs commit should not, but a pull request decides
  whether to merge, and a change to the tests or the lockfile breaks the build as surely as one to
  src. The release job is gated to push events -- releasing from a pull request would tag and
  publish a merge that has not happened.

The back-merge is the step that was missing entirely. A release on master writes a chore(release)
  commit only master has, and semantic-release reads the current version from the branch it runs on,
  so until develop is told its next release is computed from a version that is no longer the highest
  published. That is how v0.3.0-b.1 came to sit below v0.3.0 on PyPI while containing strictly more,
  and it is the same divergence that made #4 conflict.

It opens a pull request rather than pushing the merge. The conflict is not incidental: a release
  rewrites the same three files on both branches, every time, so an automatic merge would fail on
  every release and train everyone to ignore it. A PR makes the step impossible to forget while
  leaving resolution to someone who can read the changelog, and the body says which side to take and
  why. It is a no-op when develop already contains master, or when the PR is already open.


## v0.4.0-b.1 (2026-08-12)


## v0.3.0 (2026-08-05)


## v0.3.0-b.1 (2026-08-12)

### Build System

- Sync uv.lock with the version in pyproject.toml
  ([`6870a4c`](https://github.com/gaussia-labs/pyboltzmann/commit/6870a4c6f18c0ffe5b2123ef051cea8802e5bedf))

The lock still recorded 0.1.1 while pyproject had moved through three prereleases and a release.
  Nothing consumed the stale number -- the project is an editable install and the entry carries no
  hashes -- but a lockfile that disagrees with the manifest it locks is a question every reader has
  to answer before trusting the rest of it.

### Documentation

- **concepts**: Describe content on derived blocks and version skew
  ([`e7b4fd4`](https://github.com/gaussia-labs/pyboltzmann/commit/e7b4fd4663508cdd0e3f07ed860d924e945167ce))

The content section documented put_content under a heading promising content a block names, while no
  schema outside canonical had a field to name it with -- an example a reader could reasonably
  believe worked, and could not run. It now shows the payload that reaches a committed block.

Adds what a publisher and a consumer each need to know across SDK versions: which version a payload
  resolves to, what the schema-versions annotation is for, and the error an older client gets.

### Features

- **blocks**: Let semantic, episodic and procedural blocks name content
  ([`63d19c0`](https://github.com/gaussia-labs/pyboltzmann/commit/63d19c055bb9e38dcc14f63905f89d2d7cd7f512))

Only canonical could point at bytes. A semantic block was text and nothing else, so an
  interpretation whose subject is an image, a recording or any other file had no way to state what
  it claims about that file -- the datum had to be inlined into a JSON payload that is canonically
  serialized and hashed on every access, or left out.

Schema version 2 of the three derived types adds an optional content reference, through a mixin that
  carries the field and the content_digests override together. Separating them is the bug the mixin
  exists to prevent: content nothing reports is content a prune reclaims while its block still names
  it, and a redaction leaves behind.

The text stays required. searchable_text reads statement, summary and goal to answer a
  natural-language query, so a block without them would be present in the composition, provable
  against the root, and invisible to every query in the system. When the content is binary the text
  is what the block claims about it, which is the part that is knowledge.

Each version subclasses its v1, so every reader that dispatches on the v1 type keeps working
  untouched.

Provenance is deliberately left at v1. Its records are small by construction, and the removal ledger
  is the last thing that should sit behind a version barrier: it is what an auditor reads to find
  out what happened.

The emitted JSON Schema now advertises v2, so the $defs key for a block payload is named after the
  newest class. Its required fields are unchanged and content is optional, so a proposal written
  against the previous document still validates.

- **blocks**: Resolve a payload to the oldest schema that accepts it
  ([`b9c1d0d`](https://github.com/gaussia-labs/pyboltzmann/commit/b9c1d0d855a32328a646c289c349209e5e0810b0))

Selecting the newest registered schema meant that declaring a class anywhere in the process silently
  re-versioned every block written after it, including blocks using nothing the new schema added.
  Since schema_version sits inside the hashed envelope, that is a change of block_id: knowledge an
  older consumer could have read perfectly well becomes unreadable to it, to record nothing that
  consumer was missing.

Oldest-that-fits inverts the default. A payload validates against the earliest schema whose fields
  cover it, so an artifact only stops being readable by an older client at the point where it
  genuinely uses something that client has no schema for.

The candidate does not get a say. A version is not a preference, it is a statement about which
  fields the payload uses, and the payload already answers that -- so there is no new wire field and
  an older client's Candidate, which forbids extras, still parses.

_latest in ingest.schema deliberately keeps the opposite policy and now says so: what a proposer may
  *send* has to be the whole surface, or a field would be unreachable to every producer that learns
  the shape from the emitted JSON Schema.

- **distribution**: Declare each module's schema versions on the manifest
  ([`bd0893f`](https://github.com/gaussia-labs/pyboltzmann/commit/bd0893f0443dfd4548bfb6bf341c5a4a52707102))

A block commits to its schema version inside the hashed envelope, so the information was always in
  the artifact -- but only reachable by fetching and parsing envelopes, which is after the download
  a consumer may not be able to use. An old client meeting a brain written by a newer one had no way
  to find out before paying for it.

Worse, it often did not find out at all until much later: the first decode during a pull is
  conditional on the module having a rebuildable index registered, so a consumer with none installs
  the artifact cleanly and fails at the first query, with nothing tying the failure back to the
  artifact it came from.

pull now checks the declaration between resolving the manifest and fetching the config blob, so a
  refusal costs no bytes. It is scoped to the modules being installed rather than to the artifact: a
  brain whose semantic module needs a schema you lack is still installable if what you asked for is
  the episodic one, and refusing the whole thing would deny a consumer knowledge it can read to
  protect it from knowledge it never requested. An artifact published before the annotation existed
  declares nothing, and absence is read as unknown rather than as permission.

Protocol-owned annotation keys are now written after the caller's rather than before. Splatting the
  caller's last let it overwrite the protocol version -- the one annotation a consumer refuses on,
  disabled by the side that benefits from disabling it.

- **ingest**: Verify what a content reference declares, where it is written
  ([`060e052`](https://github.com/gaussia-labs/pyboltzmann/commit/060e052cbb9be60966859e0c4b6b029669e08734))

media_type was any non-empty string and size was any non-negative integer, neither checked against
  the bytes. Both are read by a consumer deciding whether to fetch content it does not hold -- so
  they are exactly the two fields it cannot verify for itself -- and both are hashed into block_id,
  which makes a wrong value permanent rather than correctable.

put_content now measures size from the bytes rather than accepting a caller's number, and requires a
  media type shaped type/subtype. It is the one point where the bytes are in hand, so the
  declaration costs nothing to check there. A payload composed by a proposer does not go through it,
  so the gate performs the same checks and additionally compares the declared size against the store
  when the brain holds the blob. Content the store does not hold yields nothing: a block may
  legitimately name bytes this brain never received, which is what a selective install produces.

A shape check rather than a registry lookup, because IANA moves without this SDK moving -- but "png"
  and " " are wrong under any registry, and they are the mistakes that happen.

Neither check is a model validator. NormalizedView extends ContentRef and takes its media type from
  a third-party normalization pipeline, so a malformed one may already sit inside a published
  canonical block_id; validating on the model would run on decode and make this SDK unable to read a
  brain an older one wrote -- strictness aimed at the past instead of the future.

### Refactoring

- **blocks**: Parse media types with the standard library
  ([`10092f9`](https://github.com/gaussia-labs/pyboltzmann/commit/10092f9c2e53ceb83413120ea69219d901119cc7))

Review feedback on #5: the hand-rolled character-class regex was the wrong tool.
  email.headerregistry is the RFC 2045 grammar as CPython implements it, so what counts as a media
  type stops being this module's opinion, and header injection and quoting are somebody else's
  solved problem. No new dependency -- this SDK ships two, and neither is for string validation.

The parser alone is not the check, though. It is defect-reporting and correctly lenient:
  'image/png;' and 'image/png; charset=utf-8' parse without complaint, and 'IMAGE/PNG' parses to the
  lowercase form. Every one of those is fine for a header and wrong for this field, because
  media_type is hashed into block_id -- a value merely equivalent to another under the RFC's
  comparison rules would give the same content two identities.

So the value has to equal what the parse reconstructs. That one comparison rejects parameters,
  trailing punctuation, stray whitespace and non-canonical case together, and refuses rather than
  normalizes for the reason LocalLayoutRegistry refuses a reference rather than rewriting it: filing
  a caller's content under a string they never passed is worse than telling them the string is
  unusable.

Adds the RFC 6838 length bound, which the regex enforced only by accident of its repetition counts,
  and coverage for the spellings the regex silently accepted.

### Testing

- Cover two live schema versions across SDK and brain
  ([`3ebe345`](https://github.com/gaussia-labs/pyboltzmann/commit/3ebe3459d9c8ba4db23fbe5fe05db69a2a7dd5af))

The registry holds two versions of one memory type for the first time, and the interesting cases are
  about a client rather than about a brain: a current SDK reading either shape, and an older SDK
  meeting a brain that uses a schema it never implemented.

The last of those was untestable before. conftest gains an autouse fixture that restores the block
  registry after each test, which retires the workaround test_content documented -- declaring a
  schema anywhere used to change the shape of every proposal of that type for the rest of the
  session -- and an old_client helper that forgets a registry entry. Nothing is mocked: the checks
  under test read that same registry, so forgetting a version is a faithful stand-in for an SDK
  without it.

Also closes the gap that made this necessary. Decoding was version-safe by construction and well
  covered; encoding was neither asserted nor visible to the golden vectors, so nothing anywhere
  stated which version a newly built block gets.


## v0.2.0 (2026-08-05)

### Documentation

- **readme**: Trim the prose to what a reader needs
  ([`6c1aef8`](https://github.com/gaussia-labs/pyboltzmann/commit/6c1aef8c79803a4c3cc23dba0785e823424f3eb6))

Cut the no-stubs/no-dead-code paragraph and the closing recap of the guides (the docs table already
  lists them), and fold the model-agnosticism note and the dependency lines into the sections they
  belong to.


## v0.2.0-b.4 (2026-08-05)

### Bug Fixes

- **brain**: Spare shared content on redaction, and make a producer drop atomic
  ([`b904bfb`](https://github.com/gaussia-labs/pyboltzmann/commit/b904bfb14651f67e75bc9255e5ba06c659594f9b))

`redact` tombstoned every digest its target named, without asking whether another block named the
  same bytes. Two canonical blocks over one blob is not exotic -- registering a source under two
  media types produces exactly that -- so redacting either destroyed the other's evidence while the
  survivor stayed a resolvable member of its composition and `verify` kept passing. Only content no
  surviving block names is destroyed now, and `redacted` reports what was held back by not listing
  it.

`drop_by_producer` looped over `drop` per module. Each call published its own snapshot, so one
  logical invalidation became N versions; the returned DropResult was the last iteration's, so every
  module dropped before it was invisible to the caller; and a policy refusal part-way through left
  the earlier drops committed with nothing to undo them. Everything is planned and authorized first,
  then written once -- the guarantee `drop` already gave for its own cascade.

`pull` also took its module list from the manifest's layers and the expected root from the config
  blob, with nothing forcing the two to agree, so an inconsistent artifact escaped as a bare
  KeyError.

- **distribution**: Bound and validate what a registry can hand a consumer
  ([`a861b4a`](https://github.com/gaussia-labs/pyboltzmann/commit/a861b4ac57c0194d5a39d70b3e0371c434f0c9a8))

`unpack_layer` read the gzip stream to exhaustion: a 398 KB layer expanded to 419 MB, a ratio the
  publisher chooses and the consumer pays. Expansion is now capped relative to the compressed size,
  so making unpacking expensive means making the download expensive first.

`parse_manifest` called `.get` on whatever `annotations` happened to be. Untrusted input means
  untrusted types, not just untrusted values.

`LocalLayoutRegistry` joined a reference onto its root and trusted the result, which an absolute or
  `..` reference walks straight out of. Refused rather than sanitised: rewriting a reference would
  file an artifact under a name nobody asked for.

- **retention**: Stop a corrupt block from making prune reclaim live evidence
  ([`cd466c4`](https://github.com/gaussia-labs/pyboltzmann/commit/cd466c46940590159b049b544f421b4311235c7e))

`mark` reached every block through a bare `except Exception`, so BlockIntegrityError was
  indistinguishable from "the bytes are gone". A single corrupt envelope therefore dropped that
  block's content from the marked set, and the sweep reclaimed evidence a retained root still named
  -- a bit flip turning into permanent loss, on the one call documented as reclaiming only what
  nothing needs.

Only absence and redaction are tolerated now. Corruption propagates and stops the sweep: a prune
  that declined can be run again once the block is restored or explicitly redacted, and a prune that
  ran cannot be undone.

- **store**: Validate layouts on open and reload tombstones when they move
  ([`963d1bf`](https://github.com/gaussia-labs/pyboltzmann/commit/963d1bfb592464d18ac80a852243469e5397212d))

The image-layout-version check lived on the `create=False` path, and `Brain.open` always passes
  `create=True` -- so the guard every caller relied on was unreachable from the public API, and a
  directory declaring a foreign layout was adopted in silence and then written into.

The tombstone map was cached for the life of the handle. A reader opened before a redaction kept the
  stale map, so `has()` answered False for a redacted digest and the block read as *missing* rather
  than *tombstoned* -- the one distinction Section 10.6 requires a store to preserve, broken by the
  ordinary case of a reader running beside a writer. It is keyed on the file's mtime and size now.

Layout files also go through one guarded reader, so a corrupt `oci-layout`, `index.json` or
  tombstone record raises ModuleError instead of leaking JSONDecodeError past the documented
  surface.

### Performance Improvements

- Derive once what three hot paths were recomputing per question
  ([`be5ce2c`](https://github.com/gaussia-labs/pyboltzmann/commit/be5ce2c61a51397e3f73e4d456494b84339f5dbb))

None of these were wrong, and all of them stopped a brain from growing.

`MerkleTree` rebuilt every sibling subtree hash from the leaves on each proof, so a
  whole-composition `verify` was quadratic: 2000 blocks took four seconds and doubling the module
  quadrupled it. Internal nodes are cached for the life of the tree -- there are O(n) of them and
  nothing is persisted, so the storage story is unchanged -- and the same 2000 blocks now take 28ms.
  Membership and index lookups stopped being linear scans too.

The cascade called `structural_dependents` per origin and per frontier block, decoding every
  semantic and procedural block each time: planning a 50-block drop in a 400-block module cost 20
  000 decodes. The structural edges are inverted once per batch, which makes planning flat in the
  number of blocks named -- 354ms to 7ms -- and the frontier walk no longer re-flattens its
  accumulated set every iteration.

The validation gate typed each candidate four times and, on a contradiction, scanned the whole
  semantic module twice for the same answer. Held blocks are grouped by the claim they make once per
  gate call, so a batch commit costs one pass over the module rather than one per candidate: 25
  candidates against a 400-block module now cost what one did.

### Testing

- Cover the audit's findings as regressions
  ([`a986c04`](https://github.com/gaussia-labs/pyboltzmann/commit/a986c04a7ed479cee42e5474d298b2be63e3e026))

Fourteen tests, none of them reachable from the existing suite, which passed throughout. Each states
  the invariant that was being violated rather than just asserting an outcome, so a change that
  reintroduces one fails against the reasoning.

The scaling tests assert on shape -- that doubling the input does not quadruple the work -- rather
  than on absolute times, so they do not become a flaky benchmark on a loaded machine.


## v0.2.0-b.3 (2026-08-05)

### Refactoring

- **merkle**: Cite RFC 9162 instead of the obsoleted RFC 6962
  ([`11d75d3`](https://github.com/gaussia-labs/pyboltzmann/commit/11d75d386b37b624a47fe1a2d4d6e3eaeedd6729))

RFC 9162 obsoletes RFC 6962, and it defines the same Merkle Tree Hash: same empty hash, same 0x00
  and 0x01 prefixes, same split at the largest power of two below n. So this moves references, not
  arithmetic. Roots, inclusion proofs and composition documents were compared against the published
  0.2.0b1 wheel over trees of 0 to 11 leaves and are identical byte for byte, and no golden vector
  changed.

Two citations were also wrong rather than merely dated. The domain separation prefixes point at
  Section 2.1.1, where the tree is defined, and the proof verification loop points at Section
  2.1.3.2, which is where that algorithm is actually written down -- RFC 6962 defined the proof and
  left verification to the reader, so the old citation named a section that did not contain the code
  beneath it.

LAYOUT_NAME stays "rfc6962-sorted/1", and a test now pins it with the reason. The string names a
  construction, not the document describing it, and the construction did not change. It also travels
  inside the composition document, which is hashed and published, and Composition.from_document
  refuses a layout it does not implement -- so renaming it would make every brain already published
  unopenable, in order to announce a change of tree that did not happen. The /1 suffix is what moves
  if the construction ever does.

BREAKING CHANGE: SortedRfc6962Layout is now SortedRfc9162Layout. Only the name changes; it is the
  same layout, computes the same roots, and reports the same layout identifier. Code that reaches
  DEFAULT_LAYOUT or the MerkleLayout protocol is unaffected. Renamed rather than aliased because an
  alias would leave the obsoleted RFC in the public surface, which is the thing this commit removes.

### Breaking Changes

- **merkle**: Sortedrfc6962layout is now SortedRfc9162Layout. Only the name changes; it is the same
  layout, computes the same roots, and reports the same layout identifier. Code that reaches
  DEFAULT_LAYOUT or the MerkleLayout protocol is unaffected. Renamed rather than aliased because an
  alias would leave the obsoleted RFC in the public surface, which is the thing this commit removes.


## v0.2.0-b.2 (2026-08-05)

### Bug Fixes

- **retention**: Report the content a snapshot names but can no longer read
  ([`5e41bfe`](https://github.com/gaussia-labs/pyboltzmann/commit/5e41bfe49ee8be019c9322eaa35f125fca458073))

A block may name bytes it does not carry, and since any schema can do that, a snapshot has a state
  nothing reported: the block is whole, its composition consistent, and the datum it names is gone.
  Every other reader is silent about it and each is right to be. `verify` skips bytes it cannot read
  -- it answers whether what is present hashes to the identity it is filed under, and has tolerated
  absent block bodies since the beginning, because a selective install is legitimate. A composition
  verifies over identities. A `prune` reclaims nothing, because a retained root still names the
  digest. So the failure surfaced at `pack_module`, which is the last place it can be found and the
  worst: by then the snapshot was believed publishable.

`resolvability` is the call whose whole job is what can still be read, so the three-way split now
  covers content too, and `is_intact` requires both halves. An episode whose transcript is gone is
  not a whole episode.

The split is three ways for content by the same argument it is for blocks (Section 10.6): a
  transcript destroyed under an erasure policy must not read as a damaged store. Redaction
  tombstones a block and its content together, so a lawful erasure stays intact.

The content of an unreadable block is not classified, because the digests it names can only be
  learned by reading it. A tombstoned block therefore contributes nothing, which is correct: what it
  named is not knowable from the snapshot, and the tombstone already says it is gone.

Reads no content. Classifying it asks the store which digests it holds, exactly as the block half
  does, so the cost stays a pass over envelopes rather than a pass over every blob.

The conformance suite requires this of any implementation. It is assertable because the seeded brain
  holds a canonical source, and canonical has named its original since the beginning -- which is
  also why this gap was never episodic-only.

BREAKING CHANGE: ResolvabilityReport.is_intact is stricter. A snapshot holding a block that names
  content the store no longer has answered True and now answers False, and the report carries three
  new fields -- content_resolvable, content_tombstoned, content_missing -- that a consumer reading
  the report as an exhaustive three-way split of block ids will not expect. The old answer was the
  bug being fixed, not a promise: such a snapshot cannot be packed for publication, so nothing that
  trusted the True was safe. Anyone asserting is_intact over a brain with absent data should expect
  it to start failing, and to find the reason in content_missing.

### Testing

- Use a generic actor in the fixtures and golden vectors
  ([`3f3892e`](https://github.com/gaussia-labs/pyboltzmann/commit/3f3892e65d175f88923e0e7cc038d5d572493b9d))

The docs stopped naming the maintainer in the last commit, but the fixtures kept `ALEX =
  Actor(id="alex")` and one golden vector still carried `Alex Fiorenza` inside a provenance record.
  A test suite is read as documentation of the API, and a conformance vector is read by every other
  implementation of the protocol, so both should say the role rather than who wrote them: the actor
  is now `curator`, named `Example Curator`.

The name lives inside the registration record of the `provenance_registration` vector, so it is part
  of what the block hashes over. Its `canonical_bytes` and `block_id` were regenerated with the
  SDK's own `canonicalize`, which moves the published id from `sha256:122f1f1b...` to
  `sha256:febdc629...`. Nothing else in the repository referenced the old id, but any other
  implementation pinned to it has to be resynced.

`pyproject.toml` keeps the real author: there the name is authorship, not an example.

### Breaking Changes

- **retention**: Resolvabilityreport.is_intact is stricter. A snapshot holding a block that names
  content the store no longer has answered True and now answers False, and the report carries three
  new fields -- content_resolvable, content_tombstoned, content_missing -- that a consumer reading
  the report as an exhaustive three-way split of block ids will not expect. The old answer was the
  bug being fixed, not a promise: such a snapshot cannot be packed for publication, so nothing that
  trusted the True was safe. Anyone asserting is_intact over a brain with absent data should expect
  it to start failing, and to find the reason in content_missing.


## v0.2.0-b.1 (2026-08-05)

### Documentation

- Document content a block names rather than carries
  ([`16e8389`](https://github.com/gaussia-labs/pyboltzmann/commit/16e83897d88b334b5e67b661a42414f93889e04f))

The Index page still showed `build(self, blocks)`, which no longer exists, so the one example a
  reader would copy was the one that would not run.

Adds `content_digests`, `put_content` and `ContentRef` to the architecture page, with the
  distinction that keeps the audit model intact: content is the block's own datum and nothing cites
  it, while evidence is canonical and everything derived from it cascades on a drop.

Both new snippets were executed against the SDK before being written down.

- Use a generic actor in the examples
  ([`37c6b0c`](https://github.com/gaussia-labs/pyboltzmann/commit/37c6b0cafd668a9c200f4d9d857271733279d89a))

Every example opened a brain as `Actor(id="alex")`, which is the maintainer's own name in
  documentation an international audience reads. `curator` says what the role is instead of who
  happened to write the page, so `actor=curator` now reads as the sentence it always meant.

An episodic block's `participants` became `["lecturer", "students"]`, which also fits the lecture
  the surrounding example is about.

The README matters most here: it is the PyPI landing page, so it is the first example anyone sees.
  Its snippet was executed again afterwards.

### Features

- **blocks**: Let any block name content it does not carry
  ([`838fb72`](https://github.com/gaussia-labs/pyboltzmann/commit/838fb724ed4e9333b28eb283a6f2480e7048e135))

A payload is JSON, canonically serialized and hashed on every access, so a datum large enough to
  matter belongs in the store with the block naming it. Canonical always worked that way. Nothing
  else could, and the concept was never first class: the three operations that must account for
  those bytes each recovered them by asking `isinstance(block, CanonicalBlock)`, which is a
  different question from the one they needed answered.

That made two of them silently wrong for any other schema that named bytes. `reachability` would not
  mark them, so `prune` deletes content a retained root still names -- data loss. `redact` would not
  destroy them, so a redaction reports success and leaves the bytes -- a compliance failure. And
  `required_blobs` would not pack them, publishing an artifact whose pointers lead nowhere. All
  three now ask the block through `content_digests`, so the schema that eventually names content
  inherits the behaviour instead of rediscovering these three bugs.

`ContentRef` generalizes a shape that was already proven: `NormalizedView` had exactly these three
  fields, and now subclasses it. It adds no field and must not, or every canonical block_id carrying
  a view would move.

The scan deliberately does not read content. It is linear and holds no reader; fetching blobs would
  turn a pass over envelopes into a pass over every blob a module holds, and the cost would arrive
  silently on a call that was cheap. Indexing content is what an index is for.

The envelope keeps its five keys and PROTOCOL_VERSION stays 1. Verified additive on both published
  surfaces: the golden vectors pass unregenerated, and the JSON Schema emitted to a model is
  byte-identical.

BREAKING CHANGE: Index.build takes a second argument, a ContentReader. An index over blocks that
  name their content cannot work from the blocks alone, and the caller previously had to construct
  the store itself and thread it in. The reader is narrower than BlockStore on purpose -- an index
  has no business calling put_bytes, tombstone or delete -- and keeps the store's own get_bytes
  name, so a BlockStore satisfies it structurally. Done now rather than through an optional hook
  because Index is a published protocol: an implementation in another language would never learn a
  hook exists.

### Breaking Changes

- **blocks**: Index.build takes a second argument, a ContentReader. An index over blocks that name
  their content cannot work from the blocks alone, and the caller previously had to construct the
  store itself and thread it in. The reader is narrower than BlockStore on purpose -- an index has
  no business calling put_bytes, tombstone or delete -- and keeps the store's own get_bytes name, so
  a BlockStore satisfies it structurally. Done now rather than through an optional hook because
  Index is a published protocol: an implementation in another language would never learn a hook
  exists.


## v0.1.1 (2026-08-04)

### Bug Fixes

- **conformance**: Let the golden vectors load without pytest
  ([`f9350c8`](https://github.com/gaussia-labs/pyboltzmann/commit/f9350c8376aeeea3d467421ee167fe4499f752a7))

The package holds two halves with opposite requirements. The golden vectors are plain JSON in the
  wheel and need nothing, because the caller they exist for writes their client in another language.
  The behavioural suites are pytest classes and need pytest.

`__init__` imported both eagerly, so the pytest requirement of the suites reached the vectors: on a
  plain `pip install pyboltzmann`, the one half designed to need nothing was the only part of the
  package that would not import. Eleven of twelve subpackages loaded; conformance was the twelfth,
  and it is the one written to be consumed by third parties.

The suites now resolve through a module `__getattr__`, and say what to install when pytest is absent
  instead of raising a bare ModuleNotFoundError from a file the caller never mentioned. The
  `conformance` extra provides it.

Found by installing the published wheel rather than testing the working tree, which is also how the
  previous release's timestamp bug escaped. The new tests run in subprocesses for the same reason:
  asking this suite whether the suites are loaded answers a different question, since it loaded
  them.


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
