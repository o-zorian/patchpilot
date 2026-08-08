from cases import normalize_extension


def test_extension_is_lowercase_without_dot() -> None:
    assert normalize_extension(".JSON") == "json"
    assert normalize_extension("txt") == "txt"
