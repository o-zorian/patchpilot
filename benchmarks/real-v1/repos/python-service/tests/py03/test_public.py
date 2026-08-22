from realbench.models import User
from realbench.parsing import serialize_user


def test_false_and_zero_are_preserved() -> None:
    assert serialize_user(User("u1", "Ada", active=False, quota=0)) == {
        "id": "u1",
        "name": "Ada",
        "active": False,
        "quota": 0,
    }
