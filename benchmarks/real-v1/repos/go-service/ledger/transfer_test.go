package ledger

import "testing"

func TestFailedCreditRollsBack(t *testing.T) {
	store := &Store{Balances: map[string]int{"a": 100, "b": 10}, FailCredit: "b"}
	if err := Transfer(store, "a", "b", 25); err == nil {
		t.Fatal("expected error")
	}
	if store.Balances["a"] != 100 || store.Balances["b"] != 10 {
		t.Fatalf("balances: %#v", store.Balances)
	}
}
