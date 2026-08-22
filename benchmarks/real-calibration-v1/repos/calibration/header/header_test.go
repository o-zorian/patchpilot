package header

import "testing"

func TestContainsWholeToken(t *testing.T) {
	if ContainsToken("gzip, br", "zip") {
		t.Fatal("substring is not a token")
	}
	if !ContainsToken("gzip, br", "br") {
		t.Fatal("missing br")
	}
}
