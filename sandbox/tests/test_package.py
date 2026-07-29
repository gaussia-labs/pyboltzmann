"""What the SDK's own test suite cannot check about itself.

Every test in ``pyboltzmann/tests`` runs against the source tree, so a file that fails to ship is invisible
from inside: the import works, the data loads, and the wheel a user installs is missing it. These tests run
against the installed distribution and against a freshly built one, which is the only place the difference
shows.

Skipped, rather than failed, when the SDK was installed from an index instead of the sibling checkout --
there is nothing to build then, and the sandbox should still be usable.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

SDK_ROOT = Path(__file__).resolve().parent.parent.parent
"""The SDK checkout this sandbox installs from, when it installs from one."""

VECTORS = ("block_ids.json", "inclusion_proofs.json", "merkle_roots.json", "serialization.json")
"""The golden vectors an implementation in another language consumes. They are the conformance suite's
input, so a wheel without them ships a suite nobody outside Python can run."""


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A freshly built wheel of the SDK."""
    if not (SDK_ROOT / "pyproject.toml").is_file():
        pytest.skip("the SDK was not installed from a sibling checkout, so there is nothing to build")

    output = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=SDK_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"building the SDK failed:\n{result.stderr}")

    built = list(output.glob("*.whl"))
    assert len(built) == 1, f"expected one wheel, got {built}"
    return built[0]


@pytest.fixture(scope="module")
def contents(wheel: Path) -> list[str]:
    """Every path inside the wheel."""
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


class TestTheInstalledPackage:
    def test_the_public_surface_imports(self) -> None:
        """A re-export that names something that moved fails here and nowhere else."""
        import boltzmann

        for name in boltzmann.__all__:
            assert hasattr(boltzmann, name), f"boltzmann.__all__ names {name}, which is not there"

    def test_the_golden_vectors_load_from_the_installed_package(self) -> None:
        from boltzmann.conformance import golden

        for name in VECTORS:
            assert golden.load(name)["vectors"], f"{name} loaded empty"

    def test_it_is_marked_as_typed(self) -> None:
        """Without py.typed a consumer gets no types from the package at all, silently."""
        import boltzmann

        assert (Path(boltzmann.__file__).parent / "py.typed").is_file()

    def test_it_is_installed_rather_than_pointed_at_the_source_tree(self) -> None:
        """``editable = false`` is the point: an editable install would hide every packaging bug above."""
        import boltzmann

        installed = Path(boltzmann.__file__).resolve()
        assert (SDK_ROOT / "src") not in installed.parents, (
            f"boltzmann resolves to {installed}, inside the source tree -- these tests would then prove "
            f"nothing about the distribution"
        )


class TestTheBuiltWheel:
    def test_py_typed_ships(self, contents: list[str]) -> None:
        assert "boltzmann/py.typed" in contents

    def test_the_golden_vectors_ship(self, contents: list[str]) -> None:
        for name in VECTORS:
            assert f"boltzmann/conformance/vectors/{name}" in contents

    def test_no_tests_ship(self, contents: list[str]) -> None:
        """They are not part of the library, and shipping them means shipping their fixtures too."""
        assert [path for path in contents if "test_" in path] == []

    def test_no_bytecode_ships(self, contents: list[str]) -> None:
        assert [path for path in contents if "__pycache__" in path or path.endswith(".pyc")] == []

    def test_the_sandbox_does_not_ship(self, contents: list[str]) -> None:
        """It lives inside the repository and must stay outside the distribution."""
        assert [path for path in contents if "sandbox" in path] == []

    def test_every_module_ships(self, contents: list[str]) -> None:
        """A module present in the source tree and absent from the wheel is an import error for a user and
        nothing at all for the suite."""
        expected = {
            f"boltzmann/{path.relative_to(SDK_ROOT / 'src' / 'boltzmann')}"
            for path in (SDK_ROOT / "src" / "boltzmann").rglob("*.py")
            if "__pycache__" not in path.parts
        }
        assert expected <= set(contents), f"missing from the wheel: {sorted(expected - set(contents))}"
