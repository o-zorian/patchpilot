package cursor

import "testing"

func TestParseRejectsInvalidLimits(t *testing.T) {
	for _, value := range []string{"0:0", "0:-1", "0:1001"} {
		if _, err := Parse(value); err == nil {
			t.Fatalf("expected %q to fail", value)
		}
	}
}
