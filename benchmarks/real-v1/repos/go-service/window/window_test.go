package window

import "testing"

func TestNormalizeZeroPage(t *testing.T) {
	page, size := Normalize(0, 500, 100)
	if page != 1 || size != 100 {
		t.Fatalf("got page=%d size=%d", page, size)
	}
}
