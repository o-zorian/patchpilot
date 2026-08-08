from cases import retry_delay


def test_retry_delay_is_capped() -> None:
    assert retry_delay(2, 10) == 4
    assert retry_delay(8, 10) == 10
