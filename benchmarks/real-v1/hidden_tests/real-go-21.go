package wrapped

import (
	"strings"
	"testing"
)

func TestLookupRetainsOperationContext(t *testing.T) {
	err := Lookup("abc")
	if err == nil || !strings.Contains(err.Error(), "lookup abc") {
		t.Fatalf("got %v", err)
	}
}
