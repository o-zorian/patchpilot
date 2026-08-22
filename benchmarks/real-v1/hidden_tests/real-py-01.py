from pathlib import Path

import pytest
from realbench.paths import safe_join


def test_sibling_with_shared_prefix_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    with pytest.raises(ValueError):
        safe_join(root, "../app-backup/secret.txt")
