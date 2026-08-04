# Documentation source

These pages are the source of truth for the Boltzmann SDK section of
[gaussia-labs/docs](https://github.com/gaussia-labs/docs). Edit them here; the
`sync-docs` workflow opens a pull request against the central repo on every push
to `master` that touches this directory.

## What travels, and what does not

`sync-docs` copies **only `*.mdx`**, preserving the directory structure, into the
`target_dir` named in `docs-sync.json` (`sdks/boltzmann`). Everything else in this
directory is local: `docs.json`, `favicon.svg` and `logo/` exist so `mint dev`
can render a preview, and the central repo keeps its own copies.

Adding or renaming a page therefore needs two edits: the file, and the
`navigation` block in `docs-sync.json`. The workflow does not rewrite the central
`docs.json`, so a genuinely new page also needs its path added to the
`Boltzmann SDK` tab there — the pull request body is the reminder.

## Internal links

Links use the path the page has **once published** — `/sdks/boltzmann/quickstart`,
not `/quickstart`. That is correct on the live site and wrong in `mint dev`, where
these pages sit at the root. Preview link targets against the central repo, or
accept the 404s locally; a link that works in the preview and 404s in production
is the worse trade.

## Preview locally

```bash
npm i -g mint
cd docs && mint dev
```

## Keep the examples true

Every snippet in `quickstart.mdx` was executed against the SDK before it was
written down. When an example changes, run it.
