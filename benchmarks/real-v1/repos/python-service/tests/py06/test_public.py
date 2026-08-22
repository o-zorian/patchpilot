from realbench.configuration import merge_config


def test_nested_override_preserves_siblings() -> None:
    base = {"http": {"timeout": 5, "retries": 2}, "debug": False}
    override = {"http": {"timeout": 10}}
    assert merge_config(base, override) == {
        "http": {"timeout": 10, "retries": 2},
        "debug": False,
    }
    assert base["http"]["timeout"] == 5
