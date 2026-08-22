from datetime import UTC, datetime

from realbench.parsing import parse_retry_after


def test_numeric_retry_after() -> None:
    assert parse_retry_after(" 15 ", datetime(2026, 1, 1, tzinfo=UTC)) == 15


def test_http_date_retry_after() -> None:
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Thu, 01 Jan 2026 00:00:09 GMT", now) == 9
