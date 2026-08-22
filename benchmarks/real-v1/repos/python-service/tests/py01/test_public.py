from pathlib import Path

import pytest
from realbench.paths import safe_join


def test_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        safe_join(tmp_path, "../escape.txt")


def test_accepts_child(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "nested/file.txt") == (tmp_path / "nested/file.txt").resolve()
