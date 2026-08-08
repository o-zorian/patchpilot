from cases import build_user_query


def test_active_filter_is_included() -> None:
    query, parameters = build_user_query(True)
    assert "active = ?" in query
    assert parameters == (True,)
