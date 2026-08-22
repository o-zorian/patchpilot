package aggregate

import "testing"

func TestCountsRetainValuesAfterSorting(t *testing.T) {
	got := Counts([]string{"z", "z", "a"})
	if len(got) != 2 || got[0] != (Count{Key: "a", Value: 1}) || got[1] != (Count{Key: "z", Value: 2}) {
		t.Fatalf("got %#v", got)
	}
}
