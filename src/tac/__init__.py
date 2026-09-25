"""tac: the toms-agent-config CLI and runner."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tac")
except PackageNotFoundError:  # a source tree on sys.path without an install
    __version__ = "0+unknown"

__all__ = ["__version__"]
