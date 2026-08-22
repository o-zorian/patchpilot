package wrapped

import (
	"errors"
	"testing"
)

func TestLookupPreservesSentinel(t *testing.T) {
	if !errors.Is(Lookup("42"), ErrNotFound) {
		t.Fatal("sentinel was lost")
	}
}
