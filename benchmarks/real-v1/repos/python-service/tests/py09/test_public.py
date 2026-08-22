import pytest
from realbench.models import Account
from realbench.services import transfer
from realbench.storage import AccountStore


def test_failed_credit_rolls_back_debit() -> None:
    store = AccountStore([Account("a", 100), Account("b", 10)], fail_credit_for="b")
    with pytest.raises(RuntimeError):
        transfer(store, "a", "b", 30)
    assert store.accounts["a"].balance == 100
    assert store.accounts["b"].balance == 10
