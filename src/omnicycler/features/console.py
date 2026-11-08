"""Console encoding for processes that run the SiLA stack."""

import contextlib
import sys


def use_utf8_console() -> None:
    """
    Write standard output and standard error as UTF-8.

    The CDK logs non-ASCII text. On a Windows console with a legacy code page
    such as cp1252, that logging fails and the CDK turns every defined or
    validation error into an undefined one. `register_standard_features`
    calls this, so every OmniCycler server is safe without `PYTHONUTF8=1`.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="backslashreplace")


__all__ = ["use_utf8_console"]
