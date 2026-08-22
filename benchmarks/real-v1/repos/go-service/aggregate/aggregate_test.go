package aggregate

import "testing"

func TestCountsHaveStableKeyOrder(t *testing.T) {
	for i := 0; i < 50; i++ {
		got := Counts([]string{"b", "a", "b", "c"})
		if len(got) != 3 || got[0].Key != "a" || got[1].Key != "b" || got[2].Key != "c" {
			t.Fatalf("unstable order: %#v", got)
		}
	}
}
