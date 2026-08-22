package cursor

import "testing"

func TestParseRejectsNegativeOffset(t *testing.T) {
	if _, err := Parse("-1:20"); err == nil {
		t.Fatal("expected negative offset to be rejected")
	}
}

func TestParseValid(t *testing.T) {
	got, err := Parse("10:20")
	if err != nil || got.Offset != 10 || got.Limit != 20 {
		t.Fatalf("unexpected result: %#v, %v", got, err)
	}
}
