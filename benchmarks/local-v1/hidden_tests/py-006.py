from cases import cache_key


def test_cache_key_is_tenant_scoped() -> None:
    assert cache_key("north", "profile") != cache_key("south", "profile")
    assert cache_key("north", "profile") == "north:profile"
