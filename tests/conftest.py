"""Shared fixtures, and the guardrails that make a second schema version testable.

``Block.__init_subclass__`` writes into a module-level registry at import time and there is no
removal API, which is what made two live schema versions awkward to test before: declaring a class
anywhere in the suite changed the shape of every proposal of that memory type for every other test
in the session. ``tests/test_content.py`` documented that trap and worked around it by patching a
property onto a real class rather than declaring a schema.

Two things here retire that workaround.

``_isolate_registry`` restores the registry after every test, so a test may register whatever it
needs without leaking. It is autouse because the failure it prevents is invisible: a leaked schema
does not fail the test that leaked it, it fails a later one, and the traceback points at the victim.

``old_client`` removes a version instead of adding one, which is how this suite simulates an SDK
that predates a schema. Nothing else can: the block registry is what "this client knows" *means*,
so the only faithful way to be an older client is to know less. It works on the module global
because ``Block.registry()`` deliberately hands out a copy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from boltzmann.blocks import base
from boltzmann.blocks.memory_type import MemoryType


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Restore the block schema registry after each test."""
    saved = dict(base._REGISTRY)
    yield
    base._REGISTRY.clear()
    base._REGISTRY.update(saved)


@pytest.fixture
def old_client() -> Callable[[MemoryType, int], None]:
    """
    Make this process behave like an SDK that never learned a schema version.

    Not a mock of the failure: it removes the registry entry that the real check consults, so a
    test exercises the same lookup a genuinely older client would perform. ``_isolate_registry``
    puts the entry back.

    Returns:
        Callable[[MemoryType, int], None]: Call with the memory type and version to forget.
    """

    def forget(memory_type: MemoryType, version: int) -> None:
        removed = base._REGISTRY.pop((memory_type, version), None)
        assert removed is not None, f"no schema registered for {memory_type.value} v{version} to forget"

    return forget
