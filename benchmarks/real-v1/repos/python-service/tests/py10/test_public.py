from realbench.security import PermissionCache


def test_permission_cache_is_scoped_to_project() -> None:
    allowed = {("u", "alpha"): True, ("u", "beta"): False}
    cache = PermissionCache()

    def loader(user: str, project: str) -> bool:
        return allowed[(user, project)]

    assert cache.authorize("u", "alpha", loader)
    assert not cache.authorize("u", "beta", loader)
