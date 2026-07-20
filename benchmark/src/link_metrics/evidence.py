"""Create-only JSON persistence for immutable benchmark evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_immutable_json(output: Path, document: dict[str, Any]) -> dict[str, Any]:
    """Write canonical JSON exactly once and return its serialized value."""
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return json.loads(serialized)
