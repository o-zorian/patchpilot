from calibration import normalize_email


def test_normalize_domain_only() -> None:
    assert normalize_email("Case.Sensitive@EXAMPLE.COM") == "Case.Sensitive@example.com"
