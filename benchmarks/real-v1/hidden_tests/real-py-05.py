from datetime import UTC, datetime

from realbench.parsing import parse_retry_after


def test_past_http_date_clamps_to_zero() -> None:
    now = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
    assert parse_retry_after("Thu, 01 Jan 2026 00:00:01 GMT", now) == 0
