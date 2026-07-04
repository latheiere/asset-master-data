from __future__ import annotations

import os
import re
from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("asset-master-data")
except PackageNotFoundError:  # Source tree used without installing the package.
    __version__ = "0+unknown"


def build_revision() -> str:
    revision = os.environ.get("MDV_GIT_SHA", "").strip().lower()
    return revision if re.fullmatch(r"[0-9a-f]{40}", revision) else "unknown"
