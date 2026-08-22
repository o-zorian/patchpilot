from realbench.models import User
from realbench.parsing import serialize_user


def test_only_none_optional_values_are_omitted() -> None:
    assert serialize_user(User("u", "N", active=False, quota=None)) == {
        "id": "u",
        "name": "N",
        "active": False,
    }
