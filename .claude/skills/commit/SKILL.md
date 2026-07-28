---
name: commit
description: Create git commits in this repo following Commitizen and Conventional Commits. Use when the user asks to commit, stage and commit, or group pending work into commits. Never adds a Co-authored-by trailer.
---

# Commit

Create commits that `commitizen` and `python-semantic-release` can parse, because this repo
derives its version and changelog from the commit history. A message that does not parse
silently drops out of the changelog and never bumps a version.

## Hard rules

1. **Never add a `Co-authored-by` trailer.** Not for Claude, not for any tool. No
   "Generated with", no attribution footer of any kind.
2. **Never use `git commit -m` with a heredoc that injects trailers.** Write the message
   and nothing else.
3. **Never `git push`** unless the user asked for it in this turn.
4. **Never `git add -A` blindly.** Stage the paths that belong to the commit you are making.

## Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

- **type**: one of `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`,
  `ci`, `chore`. Check `[tool.semantic_release.commit_parser_options]` in `pyproject.toml`
  — `minor_tags` and `patch_tags` there decide what bumps a version.
- **scope**: optional, lowercase, the area touched. Prefer the module or package name as it
  appears in the tree (`identity`, `merkle`, `store`, `ingest`, `query`, `distribution`).
- **subject**: imperative mood, lowercase, no trailing period, under 72 characters.
  "add merkle inclusion proofs", not "Added merkle inclusion proofs." or "adds…".
- **body**: optional, wrapped at 72. Say *why*, not *what* — the diff already says what.
  Worth writing when the change encodes a decision someone would otherwise undo.
- **footer**: `BREAKING CHANGE: <what breaks and what to do>` when the public surface
  changes incompatibly. This is what triggers a major bump, so do not use it loosely.

## Grouping

One commit per coherent change. Split by what a reviewer would want to read on its own, not
by directory. A test that proves a behavior belongs in the same commit as the behavior.

When several commits are pending, order them so the tree builds and tests pass at each one:
scaffolding and config first, then the layer they enable, then what depends on it.

## Procedure

1. `git status --short` and `git diff --stat` to see the whole surface.
2. Decide the grouping and state it to the user before committing.
3. For each commit: stage explicit paths, then commit.
4. `git log --oneline` at the end so the user sees the result.

Use `uv run cz check --message "<msg>"` to validate a message if unsure it parses.

## Examples

Good:

```
feat(merkle): add RFC 6962 inclusion proofs over sorted leaves

Sorting the leaves makes the root a function of the block set rather than
of insertion order, which is what lets two clients that assembled the same
blocks agree on a root. RFC 6962 is used over a naive binary tree because
it has no duplicate-leaf ambiguity (CVE-2012-2459).
```

```
fix(distribution): store the composition document when unpacking a layer

The snapshot's ModuleRef names the document by digest, so a pulled brain
pointed at a blob that was never written and could not be reopened.
```

```
refactor(protocol)!: rename publish/install to push/pull

BREAKING CHANGE: BrainDistribution.publish is now push, and install is
now pull. Both take the transport as their first argument.
```

Bad:

- `Added tests.` — wrong type, wrong mood, trailing period, no scope.
- `feat: stuff` — says nothing.
- `chore: wip` — not a commit, a savepoint.
- Anything ending in `Co-authored-by:` — forbidden here.
