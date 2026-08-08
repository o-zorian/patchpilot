from cases import sort_names


def test_names_sort_case_insensitively() -> None:
    assert sort_names(["zulu", "Alpha", "beta"]) == ["Alpha", "beta", "zulu"]
