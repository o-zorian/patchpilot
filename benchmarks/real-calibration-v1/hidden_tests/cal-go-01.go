package calcmath

import "testing"

func TestAverageNormal(t *testing.T) {
	if got := Average(9, 3); got != 3 {
		t.Fatalf("got %d", got)
	}
}
