from cases import clamp_page


def test_page_zero_is_first() -> None:
    assert clamp_page(0) == 1
    assert clamp_page(-2) == 1
    assert clamp_page(3) == 3
