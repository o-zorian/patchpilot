from calibration import normalize_email


def test_local_part_is_preserved() -> None:
    assert normalize_email("User.Name@Example.COM") == "User.Name@example.com"
