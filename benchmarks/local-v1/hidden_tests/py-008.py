from pathlib import Path

import pytest

from cases import safe_join


def test_parent_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        safe_join(tmp_path, "../escape.txt")
    assert safe_join(tmp_path, "safe.txt") == (tmp_path / "safe.txt").resolve()
