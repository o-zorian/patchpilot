from realbench.security import PermissionCache


def test_cache_key_includes_user_and_project() -> None:
    calls: list[tuple[str, str]] = []

    def loader(user: str, project: str) -> bool:
        calls.append((user, project))
        return project == "green"

    cache = PermissionCache()
    assert cache.authorize("u1", "green", loader)
    assert not cache.authorize("u1", "red", loader)
    assert cache.authorize("u2", "green", loader)
    assert calls == [("u1", "green"), ("u1", "red"), ("u2", "green")]
