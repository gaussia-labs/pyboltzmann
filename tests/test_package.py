"""Smoke tests for the package metadata, and for what importing it costs."""

import subprocess
import sys

import boltzmann


def test_version_is_exposed() -> None:
    assert isinstance(boltzmann.__version__, str)
    assert boltzmann.__version__


def test_importing_the_package_does_not_import_asyncio() -> None:
    """A reader pays for every module-scope import on every start, and asyncio alone is ~17ms.

    Only a publish needs it, to wait between re-reads of a tag, so it is imported there. In a subprocess
    because the test session has already imported asyncio by the time this runs.
    """
    program = "import sys, boltzmann, boltzmann.brain, boltzmann.distribution\nprint('asyncio' in sys.modules)\n"
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"
