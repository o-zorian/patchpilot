from realbench.parsing import parse_csv_line


def test_csv_escaped_quote_and_empty_field() -> None:
    assert parse_csv_line('"a""b",,c') == ['a"b', "", "c"]
