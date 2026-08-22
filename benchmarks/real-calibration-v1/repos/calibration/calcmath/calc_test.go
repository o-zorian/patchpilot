package calcmath

import "testing"

func TestAverageZeroCount(t *testing.T) {
	if got := Average(10, 0); got != 0 {
		t.Fatalf("got %d", got)
	}
}
