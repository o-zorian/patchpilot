package benchmarkcases

import "testing"

func TestFirstOrEmpty(t *testing.T) {
	if FirstOrEmpty(nil) != "" || FirstOrEmpty([]string{"x"}) != "x" {
		t.Fatal("empty slice was not handled")
	}
}
