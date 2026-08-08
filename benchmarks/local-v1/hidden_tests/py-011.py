from cases import merge_headers


def test_header_override_is_case_insensitive() -> None:
    merged = merge_headers({"Content-Type": "text/plain"}, {"content-type": "application/json"})
    assert merged == {"content-type": "application/json"}
