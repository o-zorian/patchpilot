from cases import safe_divide


def test_zero_denominator_returns_none() -> None:
    assert safe_divide(6, 3) == 2
    assert safe_divide(6, 0) is None
