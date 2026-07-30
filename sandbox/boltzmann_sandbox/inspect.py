"""What a registry's own interface cannot show you.

Docker Hub classifies a brain correctly -- the badge says ARTIFACT -- and then reports its content type as
*Unrecognized*, because a registry UI can only render the artifact types it was built to know. There is
nothing wrong on either side: the manifest is a valid OCI manifest, the layers are all there, and no
registry can be expected to draw an artifact type it has never heard of.

So the SDK draws it. Modules with their Merkle roots and block counts, which layers travel and which are
rebuilt, and the sizes a consumer would actually transfer -- read from the same manifest a ``pull`` reads,
either from the local layout or from the registry.

Reading the remote is deliberately the *cheap* half of a pull: one manifest request, no layers. Inspecting
what a brain contains should never mean downloading it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from boltzmann.distribution.media_types import (
    ANNOTATION_BLOCK_COUNT,
    ANNOTATION_EMBEDDING_MODEL,
    ANNOTATION_INDEX_KIND,
    ANNOTATION_MEMORY_TYPE,
    ANNOTATION_MERKLE_LAYOUT,
    ANNOTATION_MERKLE_ROOT,
    ANNOTATION_SOURCE_SNAPSHOT,
)
from boltzmann.exceptions import BoltzmannError, ReferenceNotFoundError

from boltzmann_sandbox.brain import open_brain, registry_client
from boltzmann_sandbox.config import ConfigError, load

if TYPE_CHECKING:
    from boltzmann.distribution.manifest import BrainManifest, Descriptor

    from boltzmann_sandbox.config import Settings


def human(size: int) -> str:
    """A byte count a person can compare at a glance."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def render(manifest: BrainManifest, digest: str, where: str) -> None:
    """
    Print a manifest the way a registry would if it knew what this is.

    Args:
        manifest (BrainManifest): The artifact to describe.
        digest (str): The digest it is filed under.
        where (str): Where it was read from, for the header.
    """
    print(f"\n\033[1m{where}\033[0m")
    print(f"  manifest       {digest}")
    print(f"  artifactType   {manifest.artifact_type}")
    print(f"  config         {manifest.config.media_type}")
    print(f"                 {manifest.config.digest}  {human(manifest.config.size)}")

    snapshot = manifest.annotations.get(ANNOTATION_SOURCE_SNAPSHOT)
    if snapshot is not None and snapshot != str(manifest.config.digest):
        # A projection: the config is this subset's own snapshot, and the annotation names the full version
        # it was cut from. Without it, a partial artifact looks like a brain that simply has fewer modules.
        print(f"  cut from       {snapshot}")

    modules = [layer for layer in manifest.layers if not layer.is_vector_index]
    indices = [layer for layer in manifest.layers if layer.is_vector_index]

    print(f"\n  modules ({len(modules)})")
    for layer in modules:
        _module_line(layer)

    print(f"\n  travelling indices ({len(indices)})")
    if not indices:
        print("    none -- every index this artifact needs can be rebuilt from its blocks")
    for layer in indices:
        kind = layer.annotations.get(ANNOTATION_INDEX_KIND, "?")
        model = layer.annotations.get(ANNOTATION_EMBEDDING_MODEL, "unstated")
        print(f"    {layer.annotations.get(ANNOTATION_MEMORY_TYPE, '?'):12s} {kind:8s} {human(layer.size):>9}")
        print(f"    {'':12s} built by {model}")

    total = manifest.config.size + sum(layer.size for layer in manifest.layers)
    print(f"\n  a full install transfers {human(total)}")


def _module_line(layer: Descriptor) -> None:
    """One module layer: what it holds, and the two identities it has."""
    memory_type = layer.annotations.get(ANNOTATION_MEMORY_TYPE, "?")
    blocks = layer.annotations.get(ANNOTATION_BLOCK_COUNT, "?")
    root = layer.annotations.get(ANNOTATION_MERKLE_ROOT, "(none)")
    layout = layer.annotations.get(ANNOTATION_MERKLE_LAYOUT, "(unstated)")
    print(f"    {memory_type:12s} {blocks:>4} blocks  {human(layer.size):>9}")
    # The digest names the bytes a consumer transfers; the root names the version inside them. Two
    # identities for one layer, and the pair is the point (paper Section 6.4).
    print(f"    {'':12s} layer  {layer.digest}")
    print(f"    {'':12s} root   {root}  [{layout}]")


async def inspect_remote(settings: Settings, tag: str) -> int:
    """Describe a published artifact, without downloading a single layer."""
    client = registry_client(settings)
    try:
        manifest = await client.resolve(settings.registry, tag)
    except ReferenceNotFoundError:
        print(f"{settings.registry}:{tag} is not published", file=sys.stderr)
        return 1
    except BoltzmannError as error:
        print(f"cannot read {settings.registry}:{tag}: {error}", file=sys.stderr)
        return 1

    render(manifest, str(manifest.digest), f"{settings.registry}:{tag}")
    return 0


def inspect_local(settings: Settings, tag: str) -> int:
    """Describe the local layout, packing it first so the description is of the current version."""
    brain = open_brain(settings)
    if not brain.snapshot().modules:
        print(f"{settings.brain_path} holds no version yet", file=sys.stderr)
        return 1

    manifest = brain.pack(tag=tag)
    render(manifest, str(manifest.digest), f"{settings.brain_path} (packed as {tag})")

    ready = sorted(kind.value for kind in brain.travelling_indices)
    print(f"  versions       {len(brain.ancestry())} retained")
    print(f"  index ready    {', '.join(ready) if ready else 'none'}")
    return 0


def main() -> int:
    """
    Describe a brain, local or published.

    Returns:
        int: ``0`` when the artifact was read, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="boltzmann-inspect",
        description="Show what a brain artifact contains. A registry UI cannot render one; this can.",
    )
    parser.add_argument("tag", nargs="?", help="the tag to read. Defaults to BOLTZMANN_TAG")
    parser.add_argument("--local", action="store_true", help="describe the local layout instead of the registry")
    arguments = parser.parse_args()

    try:
        settings = load()
    except ConfigError as error:
        print(f"cannot run: {error}", file=sys.stderr)
        return 1

    tag = arguments.tag or settings.tag
    if arguments.local:
        return inspect_local(settings, tag)
    return asyncio.run(inspect_remote(settings, tag))


if __name__ == "__main__":
    sys.exit(main())
