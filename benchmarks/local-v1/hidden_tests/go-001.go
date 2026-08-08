package benchmarkcases

import "testing"

func TestNormalizePageZero(t *testing.T) {
	if NormalizePage(0) != 1 || NormalizePage(-2) != 1 || NormalizePage(3) != 3 {
		t.Fatal("page values were not normalized")
	}
}
