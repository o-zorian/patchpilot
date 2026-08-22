from calibration import unique_tags


def test_unique_tags_empty_and_duplicates() -> None:
    assert unique_tags([]) == []
    assert unique_tags(["x", "x"]) == ["x"]
