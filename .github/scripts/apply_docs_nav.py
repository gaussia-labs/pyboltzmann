"""Apply the navigation an SDK declares onto the central docs.json.

The sync copies pages; nothing linked them. A page that lands with no entry pointing at it ships
unreachable -- present in the repo, absent from the site -- and the failure is invisible until
someone goes looking for a guide that is supposed to be there. Meanwhile ``docs-sync.json`` already
declares the whole tab, group by group and page by page, and only its ``target_dir`` was ever read.
So the nav was maintained twice: once as a manifest nobody applied and once by hand.

This applies the manifest, and refuses when the two disagree in either direction.

Usage:
    apply_docs_nav.py <manifest> <central-docs-checkout>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(manifest_path: str, checkout: str) -> int:
    manifest = json.loads(Path(manifest_path).read_text())
    navigation = manifest["navigation"]
    root = Path(checkout)
    config = root / "docs.json"
    document = json.loads(config.read_text())

    tabs = document["navigation"]["tabs"]
    matching = [tab for tab in tabs if tab.get("tab") == navigation["tab"]]
    if len(matching) != 1:
        print(
            f"::error::expected exactly one {navigation['tab']!r} tab in docs.json, found "
            f"{len(matching)}. Add it there once by hand; this script updates a tab, it does not "
            f"invent one.",
            file=sys.stderr,
        )
        return 1

    declared = [page for group in navigation["groups"] for page in group["pages"]]
    present = sorted(
        str(path.relative_to(root).with_suffix("")) for path in (root / manifest["target_dir"]).rglob("*.mdx")
    )

    # Both directions break something, and neither announces itself: a page missing from the nav is
    # unreachable on the site, and an entry naming a page that does not exist fails the docs build.
    # Refusing here turns both into a red workflow rather than a broken site.
    if sorted(declared) != present:
        missing = [page for page in present if page not in declared]
        dangling = [page for page in declared if page not in present]
        print(
            f"::error::docs-sync.json does not describe the pages that were synced. "
            f"Synced but not in the manifest: {missing or 'none'}. "
            f"In the manifest with no such page: {dangling or 'none'}. "
            f"Add the page to docs-sync.json (or remove the stale entry) and push again.",
            file=sys.stderr,
        )
        return 1

    duplicates = {page for page in declared if declared.count(page) > 1}
    if duplicates:
        print(f"::error::docs-sync.json lists {sorted(duplicates)} more than once", file=sys.stderr)
        return 1

    matching[0]["groups"] = navigation["groups"]
    # docs.json is stored as json.dumps(indent=2) with a trailing newline, so writing it back this
    # way changes only the tab that changed and leaves every other SDK's navigation byte-identical.
    config.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    print(f"{navigation['tab']}: {len(declared)} page(s) across {len(navigation['groups'])} group(s)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
