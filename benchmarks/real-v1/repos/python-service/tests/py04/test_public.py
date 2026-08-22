from realbench.cache import UserCache
from realbench.models import User
from realbench.services import UserService
from realbench.storage import UserStore


def test_rename_invalidates_cached_user() -> None:
    service = UserService(UserStore([User("u1", "before")]), UserCache())
    assert service.get("u1").name == "before"
    service.rename("u1", "after")
    assert service.get("u1").name == "after"
