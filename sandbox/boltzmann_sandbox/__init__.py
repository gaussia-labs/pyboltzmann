"""A sandbox for the Boltzmann SDK.

Not part of the ``boltzmann`` distribution and deliberately outside it: this is where the SDK gets
exercised as an installed package rather than as a source tree, against a real OCI registry, with the
pieces the protocol leaves to the implementer actually implemented.

Three entry points:

* ``boltzmann-doctor`` -- validate the environment before starting anything.
* ``boltzmann-mcp`` -- an MCP server exposing the protocol's operations as tools.
* ``boltzmann-demo`` -- the whole Section 11 lifecycle, end to end, with assertions.
"""

__version__ = "0.1.0"
