package window

import "testing"

func TestNormalizeNegativeAndZeroPages(t *testing.T) {
	for _, page := range []int{-2, 0} {
		got, _ := Normalize(page, 10, 100)
		if got != 1 {
			t.Fatalf("page %d normalized to %d", page, got)
		}
	}
}
