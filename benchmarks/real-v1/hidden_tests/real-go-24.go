package idempotency

import "testing"

func TestSuccessfulResultIsReused(t *testing.T) {
	state := NewState()
	calls := 0
	op := func() (int, error) { calls++; return 9, nil }
	first, err := Execute(state, "key", op)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Execute(state, "key", op)
	if err != nil || first != 9 || second != 9 || calls != 1 {
		t.Fatalf("%d %d %d %v", first, second, calls, err)
	}
}
