from realbench.models import Account
from realbench.services import transfer
from realbench.storage import AccountStore


def test_successful_transfer_commits_both_sides() -> None:
    store = AccountStore([Account("a", 100), Account("b", 10)])
    transfer(store, "a", "b", 25)
    assert (store.accounts["a"].balance, store.accounts["b"].balance) == (75, 35)
