"""Smoke tests for the package metadata."""

import boltzmann


def test_version_is_exposed() -> None:
    assert isinstance(boltzmann.__version__, str)
    assert boltzmann.__version__
