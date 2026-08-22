package ledger

import "testing"

func TestSuccessfulTransferCommits(t *testing.T) {
	store := &Store{Balances: map[string]int{"a": 100, "b": 10}}
	if err := Transfer(store, "a", "b", 30); err != nil {
		t.Fatal(err)
	}
	if store.Balances["a"] != 70 || store.Balances["b"] != 40 {
		t.Fatalf("%#v", store.Balances)
	}
}
