from calibration import unique_tags


def test_unique_tags_keep_first_seen_order() -> None:
    assert unique_tags(["b", "a", "b", "c"]) == ["b", "a", "c"]
