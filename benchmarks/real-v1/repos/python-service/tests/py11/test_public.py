import pytest
from realbench.models import Dependency
from realbench.resolver import resolve_dependencies


def test_cycle_is_reported_without_recursion_overflow() -> None:
    catalog = {
        "api": Dependency("api", ("core",)),
        "core": Dependency("core", ("api",)),
    }
    with pytest.raises(ValueError, match="cycle"):
        resolve_dependencies(["api"], catalog)


def test_dependency_order() -> None:
    catalog = {"api": Dependency("api", ("core",)), "core": Dependency("core")}
    assert resolve_dependencies(["api"], catalog) == ["core", "api"]
