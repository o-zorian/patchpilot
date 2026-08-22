from __future__ import annotations

from copy import deepcopy
from typing import Any


def clone_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(value)


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = clone_mapping(base)
    result.update(override)
    return result
