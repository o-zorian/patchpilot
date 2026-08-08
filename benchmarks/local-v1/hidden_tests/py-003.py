from cases import paginate


def test_first_page_starts_at_zero() -> None:
    assert paginate(list(range(8)), 1, 3) == [0, 1, 2]
    assert paginate(list(range(8)), 2, 3) == [3, 4, 5]
