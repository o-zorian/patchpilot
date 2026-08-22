from realbench.configuration import merge_config


def test_recursive_merge_does_not_alias_inputs() -> None:
    base = {"a": {"b": {"left": 1, "right": 2}}}
    result = merge_config(base, {"a": {"b": {"left": 9}}})
    assert result == {"a": {"b": {"left": 9, "right": 2}}}
    result["a"]["b"]["right"] = 7
    assert base["a"]["b"]["right"] == 2
