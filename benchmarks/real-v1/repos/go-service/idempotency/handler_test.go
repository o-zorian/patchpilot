package idempotency

import (
	"errors"
	"testing"
)

func TestFailureCanBeRetried(t *testing.T) {
	state := NewState()
	calls := 0
	op := func() (int, error) {
		calls++
		if calls == 1 {
			return 0, errors.New("temporary")
		}
		return 7, nil
	}
	if _, err := Execute(state, "k", op); err == nil {
		t.Fatal("expected first failure")
	}
	value, err := Execute(state, "k", op)
	if err != nil || value != 7 || calls != 2 {
		t.Fatalf("value=%d calls=%d err=%v", value, calls, err)
	}
}
