from realbench.parsing import paginate


def test_first_page_starts_with_first_item() -> None:
    assert paginate(["a", "b", "c", "d"], 1, 2) == ["a", "b"]
