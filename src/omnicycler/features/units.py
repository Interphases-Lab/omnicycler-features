"""
Every SiLA unit used by the OmniCycler standard features, defined once.

The standard features use Battery Data Format (BDF) units throughout, see
docs/decisions/0002-standard-features-use-bdf-units.md.
"""

from unitelabs.cdk.units import unit

SECOND = unit("s")
VOLT = unit("V")
AMPERE = unit("A")
AMPERE_HOUR = unit("A·h", label="Ah")
WATT_HOUR = unit("W·h", label="Wh")
DEGREE_CELSIUS = unit("°C")
PASCAL = unit("Pa")
OHM = unit("V/A", label="Ω")
HERTZ = unit("Hz")

__all__ = ["AMPERE", "AMPERE_HOUR", "DEGREE_CELSIUS", "HERTZ", "OHM", "PASCAL", "SECOND", "VOLT", "WATT_HOUR"]
