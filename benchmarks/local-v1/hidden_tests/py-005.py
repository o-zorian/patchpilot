from cases import parse_enabled


def test_json_false_string_is_false() -> None:
    assert parse_enabled({"enabled": False}) is False
    assert parse_enabled({"enabled": "false"}) is False
    assert parse_enabled({"enabled": "true"}) is True
