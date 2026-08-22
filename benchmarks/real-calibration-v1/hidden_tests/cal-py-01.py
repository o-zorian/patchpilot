from calibration import clamp


def test_clamp_above_upper() -> None:
    assert clamp(11, 0, 10) == 10
