from __future__ import annotations

from realbench.models import Dependency


def resolve_dependencies(roots: list[str], catalog: dict[str, Dependency]) -> list[str]:
    resolved: list[str] = []

    def visit(name: str) -> None:
        dependency = catalog[name]
        for required in dependency.requires:
            visit(required)
        if name not in resolved:
            resolved.append(name)

    for root in roots:
        visit(root)
    return resolved
