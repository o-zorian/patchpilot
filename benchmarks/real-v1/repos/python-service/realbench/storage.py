from __future__ import annotations

from copy import deepcopy

from realbench.models import Account, User


class UserStore:
    def __init__(self, users: list[User]) -> None:
        self._users = {user.id: user for user in users}

    def get(self, user_id: str) -> User:
        return self._users[user_id]

    def save(self, user: User) -> None:
        self._users[user.id] = user


class AccountStore:
    def __init__(self, accounts: list[Account], fail_credit_for: str | None = None) -> None:
        self.accounts = {account.id: account for account in accounts}
        self.fail_credit_for = fail_credit_for

    def debit(self, account_id: str, amount: int) -> None:
        account = self.accounts[account_id]
        if amount <= 0 or account.balance < amount:
            raise ValueError("invalid debit")
        account.balance -= amount

    def credit(self, account_id: str, amount: int) -> None:
        if account_id == self.fail_credit_for:
            raise RuntimeError("credit backend unavailable")
        self.accounts[account_id].balance += amount

    def snapshot(self) -> dict[str, Account]:
        return deepcopy(self.accounts)
