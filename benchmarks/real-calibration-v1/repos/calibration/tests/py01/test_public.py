from calibration import clamp


def test_clamp_below_lower() -> None:
    assert clamp(-1, 0, 10) == 0
