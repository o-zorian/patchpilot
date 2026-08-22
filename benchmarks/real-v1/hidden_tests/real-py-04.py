from realbench.cache import UserCache
from realbench.models import User
from realbench.services import UserService
from realbench.storage import UserStore


def test_rename_invalidates_only_target_user() -> None:
    service = UserService(
        UserStore([User("a", "A"), User("b", "B")]),
        UserCache(),
    )
    assert service.get("a").name == "A"
    assert service.get("b").name == "B"
    service.rename("a", "A2")
    assert service.get("a").name == "A2"
    assert service.get("b").name == "B"
