from realbench.parsing import parse_csv_line


def test_quoted_csv_field() -> None:
    assert parse_csv_line('one,"two,three",four') == ["one", "two,three", "four"]
