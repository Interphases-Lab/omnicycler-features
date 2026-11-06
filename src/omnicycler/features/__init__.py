"""
OmniCycler standard features.

The SiLA 2 interface every OmniCycler battery cycler server exposes, the
`CyclerBackend` interface brand servers implement, a typed async client,
a reference simulator and a conformance test kit.

Importing this package has no side effects: it opens no ports,
connections or event loops. The server side lives in
`omnicycler.features.server`, the client in `omnicycler.features.client`.
"""

from importlib.metadata import version

ORIGINATOR = "se.uu.interphaseslab"
"""The originator of every OmniCycler feature (Uppsala University, Interphases Lab)."""

CATEGORY = "cycling"
"""The category of every OmniCycler standard feature."""

__version__ = version("omnicycler-features")

__all__ = ["CATEGORY", "ORIGINATOR", "__version__"]
