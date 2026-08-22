from __future__ import annotations

from dataclasses import replace

from realbench.cache import UserCache
from realbench.storage import AccountStore, UserStore


class UserService:
    def __init__(self, store: UserStore, cache: UserCache) -> None:
        self.store = store
        self.cache = cache

    def get(self, user_id: str) -> object:
        cached = self.cache.get(user_id)
        if cached is not None:
            return cached
        user = self.store.get(user_id)
        self.cache.put(user_id, user)
        return user

    def rename(self, user_id: str, name: str) -> None:
        user = self.store.get(user_id)
        self.store.save(replace(user, name=name))


def transfer(store: AccountStore, source: str, target: str, amount: int) -> None:
    store.debit(source, amount)
    store.credit(target, amount)
