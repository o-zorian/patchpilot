from __future__ import annotations

from pathlib import Path


def safe_join(root: Path, user_path: str) -> Path:
    """Return a resolved child path or raise ValueError for traversal."""

    candidate = (root / user_path).resolve()
    if not str(candidate).startswith(str(root.resolve())):
        raise ValueError("path escapes root")
    return candidate
