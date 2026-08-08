from cases import first_or_none


def test_empty_sequence_returns_none() -> None:
    assert first_or_none([]) is None
    assert first_or_none(["a"]) == "a"
