"""Cargo output paths shared by build and release consumers."""

import os
from pathlib import Path
from typing import Mapping, Optional


def target_directory(root: Path, environment: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve CARGO_TARGET_DIR against Cargo's working directory, or use target."""
    environment = os.environ if environment is None else environment
    return root / environment.get('CARGO_TARGET_DIR', 'target')
