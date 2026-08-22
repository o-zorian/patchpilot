from realbench.parsing import paginate


def test_non_positive_pages_are_first_page() -> None:
    items = ["a", "b", "c"]
    assert paginate(items, 0, 2) == ["a", "b"]
    assert paginate(items, -9, 2) == ["a", "b"]
