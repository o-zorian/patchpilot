from realbench.models import Dependency
from realbench.resolver import resolve_dependencies


def test_unavailable_optional_dependency_is_skipped() -> None:
    catalog = {
        "app": Dependency("app", ("core", "metrics")),
        "core": Dependency("core"),
        "metrics": Dependency("metrics", ("missing-driver",), optional=True),
    }
    assert resolve_dependencies(["app"], catalog) == ["core", "app"]
