package header

import "testing"

func TestContainsTokenTrimsWhitespaceAndCase(t *testing.T) {
	if !ContainsToken(" GZip , BR ", "gzip") {
		t.Fatal("missing gzip")
	}
}
