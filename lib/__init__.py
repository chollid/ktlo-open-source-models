"""Shared contracts for the model archival pipeline."""

import sys


if sys.version_info < (3, 12):
    detected = ".".join(str(part) for part in sys.version_info[:3])
    raise RuntimeError(
        "model-archive-pipeline requires Python 3.12 or newer; "
        f"detected Python {detected}"
    )
